"""A logout does not lift a signup refusal (#114).

Logging out flushes the session. The refusal flag must carry into the
new session, and nothing else must. Made-up addresses on example.org
only.
"""

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    BROWSER,
    PASSWORD,
    HeadlessSession,
    csrf_header,
)
from apps.players.age import age_limit_date
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY

pytestmark = pytest.mark.django_db

REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


def _logout(session: HeadlessSession) -> None:
    session.client.delete(
        f"{BROWSER}/auth/session", headers=csrf_header(session.client)
    )


def test_refusal_survives_a_logout() -> None:
    session = HeadlessSession(BROWSER)
    assert session.signup(**_young()).status_code == 400

    _logout(session)
    response = session.signup()

    assert response.status_code == 400
    assert response.json()["errors"] == REFUSAL
    assert get_user_model()._default_manager.count() == 0
    assert dict(session.store().items()) == {SIGNUP_REFUSED_SESSION_KEY: True}


def test_refusal_survives_a_login_then_logout() -> None:
    account = UserFactory.create(email="signed.in@example.org", password=PASSWORD)
    EmailAddress.objects.create(user=account, email=account.email, verified=True)
    session = HeadlessSession(BROWSER)
    session.signup(**_young())
    session.post("auth/login", {"email": account.email, "password": PASSWORD})

    _logout(session)
    response = session.signup("new.player@example.org")

    assert response.json()["errors"] == REFUSAL
    assert dict(session.store().items()) == {SIGNUP_REFUSED_SESSION_KEY: True}


def test_logout_without_a_refusal_carries_nothing() -> None:
    session = HeadlessSession(BROWSER)
    session.signup()

    _logout(session)

    assert dict(session.store().items()) == {}
    assert session.signup("second.player@example.org").status_code == 401
