"""A player aged 13 to 17 starts with the display name "First L." (#221).

The age is judged with ``apps.players.age`` when the Player is created. An
adult starts with "First Last". The legal name is "First Last" for both,
and either can change the display name later (ADR 0022). Made-up names
only.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache

from apps.accounts.tests.headless import (
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
from apps.audit.models import AuditEvent
from apps.players.age import age_limit_date
from apps.players.models import Player
from apps.players.signup import SIGNUP_DETAILS_SESSION_KEY

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME_PLAYER = "/api/v1/me/player"
CLIENTS = [APP, BROWSER]
NAMES = {"first_name": "Avery", "last_name": "Nightjar"}


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


def _born(years_ago: int) -> dict[str, int]:
    """A birth year and month that make the player ``years_ago`` years old
    on the age limit date: the month before it, that many years back."""
    today = age_limit_date()
    year, month = today.year - years_ago, today.month - 1
    if month == 0:
        year, month = year - 1, 12
    return {"birth_year": year, "birth_month": month}


def _headers(session: HeadlessSession) -> dict[str, str]:
    if session.base == BROWSER:
        return csrf_header(session.client)
    return {"X-Session-Token": session.token or ""}


def _verified_session(base: str, **fields: object) -> HeadlessSession:
    session = HeadlessSession(base)
    response = session.signup(**fields)
    assert response.status_code == 401, response.json()
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    login = session.post("auth/login", {"email": SESSION_EMAIL, "password": PASSWORD})
    assert login.status_code == 200, login.json()
    return session


def _send(session: HeadlessSession, method: str, data: dict[str, Any]) -> Any:
    return session.client.generic(
        method,
        ME_PLAYER,
        json.dumps(data),
        content_type="application/json",
        headers=_headers(session),
    )


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", [13, 15, 17])
def test_a_player_aged_13_to_17_starts_with_first_name_and_initial(
    base: str, age: int
) -> None:
    session = _verified_session(base, **NAMES, **_born(age))

    response = _send(session, "POST", {})

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert player.display_name == "Avery N."
    assert player.legal_name == "Avery Nightjar"
    body = response.json()
    assert (body["display_name"], body["legal_name"]) == (
        "Avery N.",
        "Avery Nightjar",
    )


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", [18, 40])
def test_an_adult_starts_with_first_and_last_name(base: str, age: int) -> None:
    session = _verified_session(base, **NAMES, **_born(age))

    response = _send(session, "POST", {})

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert player.display_name == player.legal_name == "Avery Nightjar"


@pytest.mark.parametrize("base", CLIENTS)
def test_the_initial_is_the_last_names_first_character(base: str) -> None:
    session = _verified_session(
        base, first_name="Mary Ann", last_name="Østby Lee", **_born(14)
    )

    response = _send(session, "POST", {})

    assert response.status_code == 201, response.json()
    player = Player.objects.get()
    assert player.display_name == "Mary Ann Ø."
    assert player.legal_name == "Mary Ann Østby Lee"


@pytest.mark.parametrize("base", CLIENTS)
def test_the_longest_first_name_fits_the_minor_display_name(base: str) -> None:
    session = _verified_session(base, first_name="A" * 61, last_name="B", **_born(14))

    response = _send(session, "POST", {})

    assert response.status_code == 201, response.json()
    assert Player.objects.get().display_name == f"{'A' * 61} B."


@pytest.mark.parametrize("base", CLIENTS)
def test_a_first_name_with_no_room_for_an_initial_is_a_field_error(
    base: str,
) -> None:
    session = HeadlessSession(base)

    response = session.signup(first_name="A" * 62, last_name="B")

    assert response.status_code == 400
    [error] = response.json()["errors"]
    assert error["param"] == "first_name"
    assert error["message"] == "Your first name can be at most 61 characters."
    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_minor_cannot_show_the_full_last_name(base: str) -> None:
    session = _verified_session(base, **NAMES, **_born(15))
    created = _send(session, "POST", {})
    assert created.json()["name_format_locked"] is True

    response = _send(session, "PATCH", {"privacy": {"name_format": "full"}})

    assert response.status_code == 422
    [error] = response.json()["errors"]
    assert error == {
        "field": "body.privacy.name_format",
        "code": "invalid",
        "message": "Your full last name can be shown only from age 18.",
    }
    assert Player.objects.get().display_name == "Avery N."
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_minor_may_resend_the_stored_name_format(base: str) -> None:
    session = _verified_session(base, **NAMES, **_born(15))
    _send(session, "POST", {})

    response = _send(
        session, "PATCH", {"privacy": {"name_format": "first_last_initial"}}
    )

    assert response.status_code == 200, response.json()
    assert response.json()["display_name"] == "Avery N."
    assert not AuditEvent.objects.exists()
