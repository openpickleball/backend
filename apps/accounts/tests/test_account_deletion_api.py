"""``DELETE /api/v1/accounts/me``: a player deletes their Account (#116).

It needs a recent reauthentication, refuses staff and superusers with
409, ends every session of the Account, and frees the email address, so
the same person can sign up again and create a new Player, also when the
old Player played matches and stays in the Ledger. That is how a player
fixes a birth year and month or a gender division entered wrong (ADR
0023). Made-up data only.
"""

import json
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import enroll_totp, totp_code
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
from apps.matches.models import Team
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"
ME_PLAYER = "/api/v1/me/player"
CLIENTS = [APP, BROWSER]


def _headers(session: HeadlessSession) -> dict[str, str]:
    if session.base == BROWSER:
        return csrf_header(session.client)
    return {"X-Session-Token": session.token or ""}


def _call(session: HeadlessSession, method: str, path: str, data: Any = None) -> Any:
    send = getattr(session.client, method)
    if data is None:
        return send(path, headers=_headers(session))
    return send(
        path,
        json.dumps(data),
        content_type="application/json",
        headers=_headers(session),
    )


def _signed_up(base: str, **fields: object) -> HeadlessSession:
    """Sign up with the helper's made-up details, verify, log in, and
    create the Player."""
    session = HeadlessSession(base)
    assert session.signup(**fields).status_code == 401
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    assert session.login(SESSION_EMAIL).status_code == 200
    created = _call(session, "post", ME_PLAYER, {})
    assert created.status_code == 201, created.json()
    return session


def _play(player: Player) -> None:
    others = [PlayerFactory.create() for _ in range(3)]
    saved_match(players={Team.A: [player, others[0]], Team.B: others[1:]})


def test_signed_out_is_a_401(client: Client) -> None:
    response = client.delete(ME)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("base", CLIENTS)
def test_without_a_reauthentication_it_is_a_403(base: str) -> None:
    session = _signed_up(base)

    response = _call(session, "delete", ME)

    assert response.status_code == 403
    assert response.json()["code"] == "reauthentication_required"
    assert get_user_model().objects.get().is_active


@pytest.mark.parametrize("base", CLIENTS)
def test_a_reauthenticated_player_deletes_their_account(base: str) -> None:
    session = _signed_up(base)
    assert session.reauthenticate().status_code == 200

    response = _call(session, "delete", ME)

    assert response.status_code == 204
    assert not get_user_model().objects.filter(email=SESSION_EMAIL).exists()
    # Every session ended, this one included.
    assert _call(session, "get", ME).status_code == 401


@pytest.mark.parametrize("flag", ["is_staff", "is_superuser"])
def test_a_staff_account_is_a_409(flag: str) -> None:
    session = _signed_up(APP)
    get_user_model().objects.filter(email=SESSION_EMAIL).update(**{flag: True})
    account = get_user_model().objects.get(email=SESSION_EMAIL)
    # A staff Account needs two-factor authentication for the API.
    enrollment = enroll_totp(account)
    assert session.logout().status_code == 401
    assert (
        session.mfa_login(SESSION_EMAIL, totp_code(enrollment.secret)).status_code
        == 200
    )
    assert session.reauthenticate().status_code == 200

    response = _call(session, "delete", ME)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert get_user_model().objects.get(pk=account.pk).is_active


@pytest.mark.parametrize("base", CLIENTS)
def test_the_same_email_signs_up_again_after_playing_matches(base: str) -> None:
    session = _signed_up(base, birth_year=1991, gender_division="mens")
    old = Player.objects.get()
    _play(old)
    assert session.reauthenticate().status_code == 200
    assert _call(session, "delete", ME).status_code == 204
    # allauth sends one verification mail per address every few minutes
    # (its rate limit, in the cache): a later sign-up gets its own.
    cache.clear()

    again = _signed_up(base, birth_year=1990, gender_division="womens")

    new = Player.objects.get(account__email=SESSION_EMAIL)
    assert new.pk != old.pk
    assert (new.birth_year, new.gender_division) == (1990, "womens")
    kept = Player.objects.get(pk=old.pk)
    assert kept.birth_year is None
    assert kept.account.is_active is False
    assert _call(again, "get", ME).json() == {"email": SESSION_EMAIL}


def test_the_account_password_no_longer_logs_in() -> None:
    session = _signed_up(APP)
    _play(Player.objects.get())
    assert session.reauthenticate().status_code == 200
    assert _call(session, "delete", ME).status_code == 204

    fresh = HeadlessSession(APP)
    response = fresh.login(SESSION_EMAIL, PASSWORD)

    assert response.status_code == 400
