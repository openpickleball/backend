"""A 409 create takes the held signup details too (#124 review).

The details held at signup live only until the Account's Player exists
(ADR 0009, ADR 0017). When the Account already has one, a create gets
409, and the hold for that Account is removed then too, on both 409
paths and on both headless clients. Made-up data only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
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
from apps.players import api as players_api
from apps.players.models import Player
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SignupDetails,
    hold_signup_details,
)
from apps.players.tests.factories import PlayerFactory

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME_PLAYER = "/api/v1/me/player"


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def signed_in_session(base: str) -> HeadlessSession:
    session = HeadlessSession(base)
    assert session.signup().status_code == 401
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    login = session.post("auth/login", {"email": SESSION_EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    return session


def create(session: HeadlessSession) -> Any:
    if session.base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = {"X-Session-Token": session.token or ""}
    return session.client.post(
        ME_PLAYER,
        {"display_name": "Another Name"},
        content_type="application/json",
        headers=headers,
    )


def own_account() -> Any:
    return get_user_model()._default_manager.get_by_natural_key(SESSION_EMAIL)


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_409_create_removes_the_hold(base: str) -> None:
    session = signed_in_session(base)
    PlayerFactory.create(account=own_account(), display_name="Robin Q.")
    assert SIGNUP_DETAILS_SESSION_KEY in session.store()

    response = create(session)

    assert response.status_code == 409
    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_create_that_loses_the_race_removes_the_hold(
    base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = signed_in_session(base)
    PlayerFactory.create(account=own_account(), display_name="Robin Q.")
    monkeypatch.setattr(players_api, "_has_player", lambda account: False)

    response = create(session)

    assert response.status_code == 409
    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()
    assert Player.objects.get().display_name == "Robin Q."


def test_a_409_keeps_an_age_held_for_another_account() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    PlayerFactory.create(account=account)
    other = UserFactory.create()
    client = Client()
    client.force_login(account)
    store = client.session
    hold_signup_details(
        store, other, SignupDetails(1970, 2, "mens", "Sam Quillfeather", "", "")
    )
    store.save()

    response = client.post(
        ME_PLAYER, {"display_name": "Robin Q."}, content_type="application/json"
    )

    assert response.status_code == 409
    assert client.session[SIGNUP_DETAILS_SESSION_KEY]["account"] == other.pk


def test_a_409_whose_app_session_was_deleted_still_answers_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = signed_in_session(APP)
    PlayerFactory.create(account=own_account())

    def logged_out_meanwhile(account: Any) -> bool:
        # A logout on another request deletes the session row after auth.
        Session.objects.filter(session_key=session.token).delete()
        return True

    monkeypatch.setattr(players_api, "_has_player", logged_out_meanwhile)

    response = create(session)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
