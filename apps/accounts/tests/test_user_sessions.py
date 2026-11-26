"""Players list and end their sessions (allauth.usersessions, #100).

allauth's user sessions app keeps one row per signed-in session, with
OPEN's client IP (``CLIENT_IP_HEADER`` only) and the user agent, only
while the session lasts. OPEN's sessions view lists them with an opaque
id instead of the row id, and records one ``session.end`` Audit Event per
session a player ends. A periodic task deletes the rows of sessions that
are gone. Made-up data only.
"""

from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib import admin
from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.accounts.tasks import sweep_sessions
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent
from config import celery_app

pytestmark = pytest.mark.django_db

EMAIL = "sessions.player@example.org"
OTHER_EMAIL = "other.sessions@example.org"
IP_HEADER = settings.CLIENT_IP_HEADER
USER_AGENT = "Made-up Browser/1.0"


def _account(email: str = EMAIL) -> Any:
    account = UserFactory.create(email=email, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=email, primary=True, verified=True)
    return account


def _signed_in(
    base: str, email: str = EMAIL, ip: str = "203.0.113.10"
) -> HeadlessSession:
    session = HeadlessSession(base)
    response = session.client.post(
        f"{base}/auth/login",
        {"email": email, "password": PASSWORD},
        content_type="application/json",
        headers={**session._headers(), IP_HEADER: ip, "User-Agent": USER_AGENT},
        REMOTE_ADDR="192.0.2.50",
        HTTP_X_FORWARDED_FOR="198.51.100.77",
    )
    assert response.status_code == 200, response.content
    session._keep_token(response)
    return session


def _session_key(session: HeadlessSession) -> str:
    key = session.store().session_key
    assert key
    return key


def test_user_sessions_are_installed_without_activity_tracking() -> None:
    assert "allauth.usersessions" in settings.INSTALLED_APPS
    assert settings.USERSESSIONS_TRACK_ACTIVITY is False


def test_the_user_session_admin_is_not_registered() -> None:
    assert UserSession not in admin.site._registry


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize(
    ("ip", "stored"),
    [
        ("203.0.113.10", "203.0.113.10"),
        ("::ffff:203.0.113.11", "203.0.113.11"),
        ("2001:db8:aa:bb:1:2:3:4", "2001:db8:aa:bb::"),
        ("not an address", "0.0.0.0"),
    ],
)
def test_a_login_stores_opens_client_ip(base: str, ip: str, stored: str) -> None:
    _account()

    session = _signed_in(base, ip=ip)

    row = UserSession.objects.get(session_key=_session_key(session))
    assert row.ip == stored
    assert row.user_agent == USER_AGENT


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_list_shows_opaque_ids(base: str) -> None:
    account = _account()
    first = _signed_in(base)
    _signed_in(APP)

    response = first.get("auth/sessions")

    assert response.status_code == 200
    data = response.json()["data"]
    assert len(data) == 2
    row_ids = set(UserSession.objects.filter(user=account).values_list("pk", flat=True))
    keys = set(
        UserSession.objects.filter(user=account).values_list("session_key", flat=True)
    )
    for item in data:
        assert isinstance(item["id"], str)
        assert item["id"] not in {str(pk) for pk in row_ids}
        assert item["id"] not in keys
        assert set(item) == {"id", "ip", "user_agent", "created_at", "is_current"}
    assert sorted(item["is_current"] for item in data) == [False, True]


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_ending_another_session_records_one_event(base: str) -> None:
    account = _account()
    current = _signed_in(base)
    other = _signed_in(APP)
    other_key = _session_key(other)
    listed = current.get("auth/sessions").json()["data"]
    other_id = next(item["id"] for item in listed if not item["is_current"])

    response = current.delete_json("auth/sessions", {"sessions": [other_id]})

    assert response.status_code == 200
    assert [item["is_current"] for item in response.json()["data"]] == [True]
    assert not Session.objects.filter(session_key=other_key).exists()
    assert not UserSession.objects.filter(session_key=other_key).exists()
    assert other.get("auth/session").status_code in (401, 410)
    assert current.get("auth/session").status_code == 200
    event = AuditEvent.objects.get(action=AuditAction.SESSION_END)
    assert event.actor_user == account
    assert event.target_object_id == str(account.pk)
    assert event.after == {"current": False}
    assert event.before is None


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_ending_the_current_session_logs_out(base: str) -> None:
    _account()
    current = _signed_in(base)
    own_id = current.get("auth/sessions").json()["data"][0]["id"]

    response = current.delete_json("auth/sessions", {"sessions": [own_id]})

    assert response.status_code == 401
    event = AuditEvent.objects.get(action=AuditAction.SESSION_END)
    assert event.after == {"current": True}
    assert not UserSession.objects.exists()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_an_unknown_or_foreign_id_ends_nothing(base: str) -> None:
    _account()
    _account(OTHER_EMAIL)
    current = _signed_in(base)
    foreign = _signed_in(APP, email=OTHER_EMAIL)
    foreign_id = foreign.get("auth/sessions").json()["data"][0]["id"]
    rows = UserSession.objects.count()

    for sent in (
        foreign_id,
        "not-an-id",
        1,
        str(UserSession.objects.values_list("pk", flat=True)[0]),
    ):
        response = current.delete_json("auth/sessions", {"sessions": [sent]})
        assert response.status_code == 400
        assert str(sent) not in response.content.decode()

    assert UserSession.objects.count() == rows
    assert not AuditEvent.objects.filter(action=AuditAction.SESSION_END).exists()


def test_the_sessions_view_needs_sign_in() -> None:
    assert HeadlessSession(APP).get("auth/sessions").status_code == 401
    assert HeadlessSession(BROWSER).get("auth/sessions").status_code == 401


def test_logout_deletes_the_session_row() -> None:
    _account()
    session = _signed_in(BROWSER)
    assert UserSession.objects.count() == 1

    session.logout()

    assert not UserSession.objects.exists()


def test_the_sweep_deletes_rows_of_ended_or_expired_sessions() -> None:
    account = _account()
    live = _signed_in(BROWSER)
    gone = _signed_in(APP)
    expired = _signed_in(APP)
    Session.objects.filter(session_key=_session_key(gone)).delete()
    Session.objects.filter(session_key=_session_key(expired)).update(
        expire_date=timezone.now() - timedelta(seconds=1)
    )

    deleted = sweep_sessions()

    assert deleted == 2
    assert list(
        UserSession.objects.filter(user=account).values_list("session_key", flat=True)
    ) == [_session_key(live)]
    assert not Session.objects.filter(expire_date__lte=timezone.now()).exists()


def test_the_sweep_runs_every_15_minutes_on_the_default_queue() -> None:
    [entry] = [
        entry
        for entry in celery_app.conf.beat_schedule.values()
        if entry["task"] == sweep_sessions.name
    ]

    assert sweep_sessions.name == "apps.accounts.tasks.sweep_sessions"
    schedule = entry["schedule"]
    assert schedule.minute == {0, 15, 30, 45}
    assert schedule.hour == set(range(24))
    assert entry["options"] == {"expire_seconds": 900}
