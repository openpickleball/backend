"""A create whose app session ends mid-request still answers 201 (#124 review).

The app client's session is the one its ``X-Session-Token`` names. If a
concurrent logout deletes that session row after auth, saving the taken
signup details raises ``UpdateError``. The Player is already created, and the
session, with its hold, is gone, so the answer is still 201. Made-up data
only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.contrib.sessions.models import Session
from django.core.cache import cache

from apps.accounts.tests.headless import (
    APP,
    PASSWORD,
    SESSION_EMAIL,
    VERIFY_PREFIX,
    HeadlessSession,
    key_from_link,
    link_in_last_mail,
)
from apps.players import api as players_api
from apps.players.models import Player
from apps.players.services import create_player

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def app_session() -> HeadlessSession:
    session = HeadlessSession(APP)
    assert session.signup().status_code == 401
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    session.post("auth/email/verify", {"key": key})
    login = session.post("auth/login", {"email": SESSION_EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    return session


def test_a_create_whose_session_was_deleted_still_answers_201(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = app_session()

    def create_then_log_out(**kwargs: Any) -> Player:
        player = create_player(**kwargs)
        # A logout on another request deletes the session row.
        Session.objects.filter(session_key=session.token).delete()
        return player

    monkeypatch.setattr(players_api, "create_player", create_then_log_out)

    response = session.client.post(
        "/api/v1/me/player",
        {"display_name": "Robin Q."},
        content_type="application/json",
        headers={"X-Session-Token": session.token or ""},
    )

    assert response.status_code == 201
    assert Player.objects.filter(public_code=response.json()["code"]).exists()
    assert not Session.objects.filter(session_key=session.token).exists()
