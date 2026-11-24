"""The Player created after signup gets the signup's name and place (#221).

The held legal name ("First Last") becomes both the Player's legal name
and its starting display name, with the held city and region, and the
body's values are not used (ADR 0021). With nothing held, the body's
display name is required. The legal name never reaches an Audit Event or
a log. Made-up names only.
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    SESSION_EMAIL,
    SIGNUP_FULL_NAME,
    VERIFY_PREFIX,
    HeadlessSession,
    csrf_header,
    key_from_link,
    link_in_last_mail,
)
from apps.audit.models import AuditEvent
from apps.players.models import Player
from apps.players.signup import SIGNUP_DETAILS_SESSION_KEY

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME_PLAYER = "/api/v1/me/player"
CLIENTS = [APP, BROWSER]


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


def _verified_session(base: str, **fields: object) -> HeadlessSession:
    """Sign up with ``fields`` over the helper's details, verify, log in."""
    session = HeadlessSession(base)
    assert session.signup(**fields).status_code == 401
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    login = session.post("auth/login", {"email": SESSION_EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.json()
    return session


def _create(session: HeadlessSession, data: dict[str, Any]) -> Any:
    if session.base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = {"X-Session-Token": session.token or ""}
    return session.client.post(
        ME_PLAYER, json.dumps(data), content_type="application/json", headers=headers
    )


@pytest.mark.parametrize("base", CLIENTS)
def test_the_player_gets_the_signup_name_and_place(base: str) -> None:
    session = _verified_session(
        base, first_name="Avery", last_name="Nightjar", city="Riverbend", region="Ohio"
    )

    response = _create(session, {})

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert player.legal_name == player.display_name == "Avery Nightjar"
    assert (player.city, player.region) == ("Riverbend", "Ohio")
    body = response.json()
    assert body["legal_name"] == body["display_name"] == "Avery Nightjar"
    assert (body["city"], body["region"]) == ("Riverbend", "Ohio")
    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_signup_without_a_place_gives_a_player_without_one(base: str) -> None:
    session = _verified_session(base, city=..., region=...)

    response = _create(session, {})

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert player.display_name == SIGNUP_FULL_NAME
    assert (player.city, player.region) == ("", "")


@pytest.mark.parametrize("base", CLIENTS)
def test_the_held_name_and_place_win_over_the_body(base: str) -> None:
    session = _verified_session(base)

    response = _create(
        session,
        {
            "display_name": "Body Name",
            "legal_name": "Body Legal",
            "city": "Body City",
            "region": "Body Region",
        },
    )

    assert response.status_code == 201
    player = Player.objects.get()
    assert player.display_name == player.legal_name == SIGNUP_FULL_NAME
    assert "Body" not in f"{player.city} {player.region}"


@pytest.mark.parametrize("base", CLIENTS)
def test_the_player_can_change_the_starting_display_name(base: str) -> None:
    session = _verified_session(base)
    _create(session, {})
    if base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = {"X-Session-Token": session.token or ""}

    response = session.client.patch(
        ME_PLAYER,
        json.dumps({"display_name": "Robin Q."}),
        content_type="application/json",
        headers=headers,
    )

    assert response.status_code == 200, response.json()
    player = Player.objects.get()
    assert player.display_name == "Robin Q."
    assert player.legal_name == SIGNUP_FULL_NAME


@pytest.mark.parametrize("base", CLIENTS)
def test_the_legal_name_reaches_no_audit_event_or_log(
    base: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session = _verified_session(base, first_name="Avery", last_name="Nightjar")

    _create(session, {})
    _create(session, {})

    assert Player.objects.get().legal_name == "Avery Nightjar"
    for event in AuditEvent.objects.all():
        values = json.dumps([event.before, event.after, event.reason])
        assert "Nightjar" not in values
    assert "Nightjar" not in caplog.text


# With nothing held, the body gives every value


def _signed_in_verified() -> Client:
    account = UserFactory.create(email="made.up.player@example.org")
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    client = Client()
    client.force_login(account)
    return client


@pytest.mark.parametrize("display_name", [..., None])
def test_with_no_hold_a_missing_display_name_gets_422(display_name: object) -> None:
    data: dict[str, object] = {
        "birth_year": 1985,
        "birth_month": 3,
        "gender_division": "mens",
    }
    if display_name is not ...:
        data["display_name"] = display_name

    response = _signed_in_verified().post(
        ME_PLAYER, data, content_type="application/json"
    )

    assert response.status_code == 422
    assert [
        (error["field"], error["code"], error["message"])
        for error in response.json()["errors"]
    ] == [("body.display_name", "missing", "Enter a display name.")]
    assert not Player.objects.exists()


def test_with_no_hold_the_body_gives_the_name_and_place() -> None:
    response = _signed_in_verified().post(
        ME_PLAYER,
        {
            "display_name": "Robin Q.",
            "legal_name": "Robin Quillfeather",
            "birth_year": 1985,
            "birth_month": 3,
            "gender_division": "mens",
            "city": "Lakeview",
        },
        content_type="application/json",
    )

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert (player.display_name, player.legal_name) == (
        "Robin Q.",
        "Robin Quillfeather",
    )
    assert (player.city, player.region) == ("Lakeview", "")
