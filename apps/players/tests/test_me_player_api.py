"""The signed-in Account's own Player at /api/v1/me/player (issue #124).

POST creates it, once per Account, for an Account whose own address is
verified. The birth year and month come from the age the signup held in
the session (ADR 0017), on either headless client, and from the body
only when the session holds none. GET returns it and PATCH changes its
profile fields and privacy settings through the services. Made-up data
only.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    ADULT_BIRTH,
    APP,
    BROWSER,
    PASSWORD,
    SESSION_EMAIL,
    VERIFY_PREFIX,
    HeadlessSession,
    csrf_header,
    key_from_link,
    link_in_last_mail,
)
from apps.players import api as players_api
from apps.players.age import SIGNUP_BIRTH_SESSION_KEY, age_limit_date, hold_signup_birth
from apps.players.models import Player, Visibility
from apps.players.tests.factories import PlayerFactory

# allauth's account emails are sent when each request returns (ADR 0016).
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME_PLAYER = "/api/v1/me/player"
CLIENTS = [APP, BROWSER]
HELD = (ADULT_BIRTH["birth_year"], ADULT_BIRTH["birth_month"])
FORBIDDEN = {
    "code": "forbidden",
    "message": "Verify your email address before creating your player.",
    "errors": [],
}
CONFLICT = {
    "code": "conflict",
    "message": "This account already has a player.",
    "errors": [],
}


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


# Helpers


def verified_session(base: str) -> HeadlessSession:
    """A headless session that signed up with an adult age, verified the
    address, and logged in, all in one session."""
    session = HeadlessSession(base)
    assert session.signup().status_code == 401
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    login = session.post("auth/login", {"email": SESSION_EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.json()
    return session


def call(session: HeadlessSession, method: str, data: Any = None) -> Any:
    """Call /api/v1/me/player through a headless session's credentials."""
    if session.base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = {"X-Session-Token": session.token or ""}
    return session.client.generic(
        method,
        ME_PLAYER,
        "" if data is None else json.dumps(data),
        content_type="application/json",
        headers=headers,
    )


def verified_account(email: str = "made.up.player@example.org") -> Any:
    account = UserFactory.create(email=email)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def post(client: Client, data: dict[str, Any]) -> Any:
    return client.post(ME_PLAYER, data, content_type="application/json")


def fields(response: Any) -> dict[str, tuple[str, str]]:
    return {
        error["field"]: (error["code"], error["message"])
        for error in response.json()["errors"]
    }


# POST: through the headless flows, with the age held at signup


@pytest.mark.parametrize("base", CLIENTS)
def test_create_takes_the_age_held_at_signup(base: str) -> None:
    session = verified_session(base)

    response = call(session, "POST", {"display_name": "Robin Q."})

    assert response.status_code == 201, response.json()
    body = response.json()
    player = Player.objects.get()
    assert body["code"] == player.public_code
    assert (body["birth_year"], body["birth_month"]) == HELD
    assert (player.birth_year, player.birth_month) == HELD
    assert player.account.email == SESSION_EMAIL
    assert SIGNUP_BIRTH_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_the_session_still_works_after_create(base: str) -> None:
    session = verified_session(base)
    call(session, "POST", {"display_name": "Robin Q."})

    response = session.client.get(
        "/api/v1/accounts/me",
        headers={} if base == BROWSER else {"X-Session-Token": session.token or ""},
    )

    assert response.status_code == 200


@pytest.mark.parametrize("base", CLIENTS)
def test_the_held_age_wins_over_the_body(base: str) -> None:
    session = verified_session(base)

    response = call(
        session,
        "POST",
        {"display_name": "Robin Q.", "birth_year": 1980, "birth_month": 1},
    )

    assert response.status_code == 201
    assert (response.json()["birth_year"], response.json()["birth_month"]) == HELD


@pytest.mark.parametrize("base", CLIENTS)
def test_a_failed_create_keeps_the_held_age_for_a_retry(base: str) -> None:
    session = verified_session(base)

    refused = call(session, "POST", {"display_name": "   "})

    assert refused.status_code == 422
    assert "body.display_name" in fields(refused)
    assert not Player.objects.exists()
    assert session.store()[SIGNUP_BIRTH_SESSION_KEY]["year"] == HELD[0]

    retried = call(session, "POST", {"display_name": "Robin Q."})

    assert retried.status_code == 201
    assert (retried.json()["birth_year"], retried.json()["birth_month"]) == HELD


# POST: with no age held


def test_with_no_hold_the_body_gives_the_age() -> None:
    client = signed_in(verified_account())

    response = post(
        client, {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3}
    )

    assert response.status_code == 201
    assert (response.json()["birth_year"], response.json()["birth_month"]) == (
        1985,
        3,
    )


def test_an_age_held_for_another_account_is_not_used() -> None:
    account = verified_account()
    other = UserFactory.create()
    client = signed_in(account)
    session = client.session
    hold_signup_birth(session, other, 1970, 2)
    session.save()

    response = post(
        client, {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3}
    )

    assert response.status_code == 201
    assert response.json()["birth_year"] == 1985
    assert client.session[SIGNUP_BIRTH_SESSION_KEY]["account"] == other.pk


def test_with_no_hold_a_missing_age_gets_422_on_each_field() -> None:
    client = signed_in(verified_account())

    response = post(client, {"display_name": "Robin Q."})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert fields(response) == {
        "body.birth_year": ("missing", "Enter your birth year."),
        "body.birth_month": ("missing", "Enter your birth month."),
    }
    assert not Player.objects.exists()


def test_with_no_hold_a_missing_month_gets_422_on_the_month_only() -> None:
    client = signed_in(verified_account())

    response = post(client, {"display_name": "Robin Q.", "birth_year": 1985})

    assert response.status_code == 422
    assert fields(response) == {
        "body.birth_month": ("missing", "Enter your birth month."),
    }


def test_an_under_13_age_gets_a_fixed_422_with_no_age_in_it() -> None:
    client = signed_in(verified_account())
    this_year = age_limit_date().year

    response = post(
        client,
        {"display_name": "Robin Q.", "birth_year": this_year - 5, "birth_month": 1},
    )

    assert response.status_code == 422
    errors = fields(response)
    assert errors["body.birth_month"][1] == "This birth year and month can't be used."
    assert str(this_year - 5) not in response.content.decode()
    assert not Player.objects.exists()


def test_a_player_aged_13_to_17_starts_with_every_setting_private() -> None:
    client = signed_in(verified_account())
    this_year = age_limit_date().year

    response = post(
        client,
        {"display_name": "Robin Q.", "birth_year": this_year - 15, "birth_month": 1},
    )

    assert response.status_code == 201
    assert set(response.json()["privacy"].values()) == {Visibility.PRIVATE}


def test_create_takes_the_optional_profile_fields() -> None:
    client = signed_in(verified_account())

    response = post(
        client,
        {
            "display_name": "Robin Q.",
            "birth_year": 1985,
            "birth_month": 3,
            "legal_name": "Robin Quill",
            "gender_division": "womens",
            "city": "Springfield",
            "region": "Ohio",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["legal_name"] == "Robin Quill"
    assert body["gender_division"] == "womens"
    assert (body["city"], body["region"]) == ("Springfield", "Ohio")
    assert body["privacy"] == {
        "city_region_visibility": "signed_in",
        "match_history_visibility": "signed_in",
        "rating_history_visibility": "signed_in",
        "stats_visibility": "signed_in",
        "home_club_visibility": "signed_in",
    }


def test_an_invalid_value_gets_a_message_without_the_value() -> None:
    client = signed_in(verified_account())

    response = post(
        client,
        {
            "display_name": "Robin Q.",
            "birth_year": 1985,
            "birth_month": 3,
            "gender_division": "made-up-division",
        },
    )

    assert response.status_code == 422
    assert fields(response)["body.gender_division"][1] == (
        "Choose one of the listed options."
    )
    assert "made-up-division" not in response.content.decode()


def test_a_non_string_display_name_gets_422_without_the_value() -> None:
    client = signed_in(verified_account())

    response = post(
        client, {"display_name": 98765, "birth_year": 1985, "birth_month": 3}
    )

    assert response.status_code == 422
    assert "body.display_name" in fields(response)
    assert "98765" not in response.content.decode()


def test_an_unknown_key_gets_422() -> None:
    client = signed_in(verified_account())

    response = post(
        client,
        {
            "display_name": "Robin Q.",
            "birth_year": 1985,
            "birth_month": 3,
            "account": 1,
        },
    )

    assert response.status_code == 422
    assert "body.account" in fields(response)
    assert not Player.objects.exists()


# POST: one Player per Account, verified address only


def test_a_second_create_gets_409_and_creates_nothing() -> None:
    account = verified_account()
    client = signed_in(account)
    data = {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3}
    assert post(client, data).status_code == 201

    response = post(client, {**data, "display_name": "Another Name"})

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert Player.objects.get().display_name == "Robin Q."


def test_a_second_create_gets_409_even_with_no_age() -> None:
    client = signed_in(verified_account())
    post(client, {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3})

    response = post(client, {"display_name": "Robin Q."})

    assert response.status_code == 409
    assert response.json() == CONFLICT


def test_a_create_that_loses_a_race_gets_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two concurrent creates both pass the first check; create_player's
    # lock makes the second one fail its one-Player check.
    account = verified_account()
    PlayerFactory.create(account=account, display_name="Robin Q.")
    monkeypatch.setattr(players_api, "_has_player", lambda account: False)

    response = post(
        signed_in(account),
        {"display_name": "Another Name", "birth_year": 1985, "birth_month": 3},
    )

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert Player.objects.get().display_name == "Robin Q."


def test_an_unverified_address_gets_403_and_keeps_the_hold() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=False, primary=True
    )
    client = signed_in(account)
    session = client.session
    hold_signup_birth(session, account, *HELD)
    session.save()

    response = post(client, {"display_name": "Robin Q."})

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert not Player.objects.exists()
    assert client.session[SIGNUP_BIRTH_SESSION_KEY]["account"] == account.pk


def test_an_account_with_no_address_row_gets_403() -> None:
    response = post(
        signed_in(UserFactory.create()),
        {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3},
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN


def test_a_verified_other_address_does_not_count() -> None:
    account = UserFactory.create(email="login.address@example.org")
    EmailAddress.objects.create(
        user=account, email="old.address@example.org", verified=True
    )

    response = post(
        signed_in(account),
        {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3},
    )

    assert response.status_code == 403
    assert not Player.objects.exists()


def test_a_signed_out_create_gets_401() -> None:
    response = post(Client(), {"display_name": "Robin Q."})

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_browser_create_without_csrf_gets_403() -> None:
    session = verified_session(BROWSER)

    response = session.client.post(
        ME_PLAYER, {"display_name": "Robin Q."}, content_type="application/json"
    )

    assert response.status_code == 403
    assert not Player.objects.exists()


def test_the_account_is_unchanged_by_a_create() -> None:
    account = verified_account()
    post(
        signed_in(account),
        {"display_name": "Robin Q.", "birth_year": 1985, "birth_month": 3},
    )

    stored = get_user_model().objects.get(pk=account.pk)
    assert stored.email == account.email
    assert stored.player.public_code
