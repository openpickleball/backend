"""An under-13 Player create refuses the session, like sign-up (issue #220).

``POST /api/v1/me/player`` with an under-13 age sets the signup refusal
flag on the session behind the request (ADR 0017), and honors it: every
later create in that session, whatever its age, gets the same 422. The
flag carries across sign-up, login, and logout on both headless clients.
Made-up data only.
"""

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
    HeadlessSession,
    app_headers,
    csrf_header,
)
from apps.players.age import age_limit_date
from apps.players.models import Player
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SIGNUP_REFUSED_SESSION_KEY,
    SignupDetails,
    hold_signup_details,
)
from apps.players.tests.factories import PlayerFactory
from apps.players.tests.test_me_player_api import call

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

CLIENTS = [APP, BROWSER]
ME_PLAYER = "/api/v1/me/player"
ACCOUNT_EMAIL = "refused.player@example.org"
DIVISION = {"gender_division": "mens"}
ADULT_BODY = {"display_name": "Robin Q.", **ADULT_BIRTH, **DIVISION}
REFUSED_ERRORS = [
    {
        "field": "body.birth_month",
        "code": "invalid",
        "message": "This birth year and month can't be used.",
    }
]
SIGNUP_REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


def young_birth() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 1}


def young_body(**fields: object) -> dict[str, object]:
    return {"display_name": "Robin Q.", **young_birth(), **DIVISION, **fields}


def verified_account(email: str = ACCOUNT_EMAIL) -> Any:
    account = UserFactory.create(email=email, password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def log_in(session: HeadlessSession, email: str = ACCOUNT_EMAIL) -> None:
    response = session.post("auth/login", {"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.json()


def logged_in_session(base: str) -> HeadlessSession:
    """A session logged in to a verified Account, with no details held."""
    verified_account()
    session = HeadlessSession(base)
    log_in(session)
    return session


def log_out(session: HeadlessSession) -> None:
    """Log out, keeping the app client's new token from the response."""
    if session.base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = app_headers(session.token)
    response = session.client.delete(f"{session.base}/auth/session", headers=headers)
    assert response.status_code == 401
    if session.base == APP:
        session.token = response.json().get("meta", {}).get("session_token")


def current_account(session: HeadlessSession) -> Any:
    headers = {} if session.base == BROWSER else app_headers(session.token)
    return session.client.get("/api/v1/accounts/me", headers=headers)


# Setting the flag


@pytest.mark.parametrize("base", CLIENTS)
def test_an_under_13_create_flags_the_session(base: str) -> None:
    session = logged_in_session(base)

    response = call(session, "POST", young_body())

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS
    assert session.store().get(SIGNUP_REFUSED_SESSION_KEY) is True
    assert current_account(session).status_code == 200
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_an_adult_retry_gets_the_same_answer(base: str) -> None:
    session = logged_in_session(base)

    first = call(session, "POST", young_body())
    retry = call(session, "POST", ADULT_BODY)

    assert retry.status_code == first.status_code == 422
    assert retry.content == first.content
    assert retry.json()["errors"] == REFUSED_ERRORS
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_the_first_refusal_hides_other_errors(base: str) -> None:
    session = logged_in_session(base)

    first = call(session, "POST", young_body(display_name=" "))
    retry = call(session, "POST", ADULT_BODY)

    assert first.status_code == 422
    assert first.json()["errors"] == REFUSED_ERRORS
    assert first.content == retry.content
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_the_flag_is_the_only_new_session_value(base: str) -> None:
    session = logged_in_session(base)
    before = set(session.store().keys())

    call(session, "POST", young_body())

    store = session.store()
    assert set(store.keys()) - before == {SIGNUP_REFUSED_SESSION_KEY}
    assert SIGNUP_DETAILS_SESSION_KEY not in store


@pytest.mark.parametrize("base", CLIENTS)
def test_a_flagged_session_loses_its_held_details(base: str) -> None:
    session = logged_in_session(base)
    store = session.store()
    store[SIGNUP_REFUSED_SESSION_KEY] = True
    account = get_user_model()._default_manager.get_by_natural_key(ACCOUNT_EMAIL)
    hold_signup_details(
        store,
        account,
        SignupDetails(
            birth_year=ADULT_BIRTH["birth_year"],
            birth_month=ADULT_BIRTH["birth_month"],
            gender_division="mens",
            legal_name="Sam Quillfeather",
            city="",
            region="",
        ),
    )
    store.save()

    response = call(session, "POST", {"display_name": "Robin Q."})

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS
    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()
    assert not Player.objects.exists()


# Across sign-up, login, and logout


@pytest.mark.parametrize("base", CLIENTS)
def test_a_signup_refusal_blocks_a_later_create(base: str) -> None:
    verified_account()
    session = HeadlessSession(base)
    refused = session.signup("young.player@example.org", **young_birth())
    assert refused.json()["errors"] == SIGNUP_REFUSAL
    log_in(session)

    response = call(session, "POST", ADULT_BODY)

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_create_refusal_blocks_a_later_signup(base: str) -> None:
    session = logged_in_session(base)
    call(session, "POST", young_body())
    log_out(session)

    response = session.signup("new.player@example.org")

    assert response.status_code == 400
    assert response.json()["errors"] == SIGNUP_REFUSAL


@pytest.mark.parametrize("base", CLIENTS)
def test_the_flag_survives_logout_and_login(base: str) -> None:
    session = logged_in_session(base)
    call(session, "POST", young_body())
    log_out(session)
    log_in(session)

    response = call(session, "POST", ADULT_BODY)

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS
    assert not Player.objects.exists()


# What does not set the flag


def _future_birth() -> dict[str, int]:
    return {"birth_year": age_limit_date().year + 1, "birth_month": 1}


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {"display_name": "Robin Q.", "birth_year": 2000, **DIVISION},
            id="missing-month",
        ),
        pytest.param(
            {"display_name": "Robin Q.", **_future_birth(), **DIVISION},
            id="future-month",
        ),
        pytest.param(
            {
                "display_name": "Robin Q.",
                "birth_year": "2000",
                "birth_month": 1,
                **DIVISION,
            },
            id="string-year",
        ),
    ],
)
def test_other_age_errors_do_not_flag(base: str, body: dict[str, object]) -> None:
    session = logged_in_session(base)

    refused = call(session, "POST", body)
    created = call(session, "POST", ADULT_BODY)

    assert refused.status_code == 422
    assert refused.json()["errors"] != REFUSED_ERRORS
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()
    assert created.status_code == 201, created.json()


# Order: 403 and 409 come before the flag


def _flagged(client: Client) -> None:
    store = client.session
    store[SIGNUP_REFUSED_SESSION_KEY] = True
    store.save()


def test_a_flagged_unverified_account_gets_403() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=False, primary=True
    )
    client = Client()
    client.force_login(account)
    _flagged(client)

    response = client.post(ME_PLAYER, ADULT_BODY, content_type="application/json")

    assert response.status_code == 403


def test_a_flagged_account_with_a_player_gets_409() -> None:
    account = verified_account()
    PlayerFactory.create(account=account)
    client = Client()
    client.force_login(account)
    _flagged(client)

    response = client.post(ME_PLAYER, ADULT_BODY, content_type="application/json")

    assert response.status_code == 409
