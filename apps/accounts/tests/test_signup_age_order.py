"""allauth's 409 and 403 come before the age screen (#114).

A signed-in session gets allauth's 409 and a closed signup its 403,
whatever age is sent, and neither flags the session. Made-up addresses
on example.org only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.adapter import AccountAdapter
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.players.age import age_limit_date
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
SIGNED_IN = "signed.in@example.org"


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


AGES: list[Any] = ["young", {"birth_month": 13}]


def _age(age: Any) -> dict[str, Any]:
    return _young() if age == "young" else dict(age)


def _signed_in(base: str) -> HeadlessSession:
    account = UserFactory.create(email=SIGNED_IN, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=SIGNED_IN, verified=True)
    session = HeadlessSession(base)
    response = session.post("auth/login", {"email": SIGNED_IN, "password": PASSWORD})
    assert response.status_code == 200
    return session


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", AGES)
def test_a_signed_in_session_gets_409_and_no_flag(base: str, age: Any) -> None:
    session = _signed_in(base)

    response = session.signup("new.player@example.org", **_age(age))

    assert response.status_code == 409
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", AGES)
def test_a_closed_signup_gets_403_and_no_flag(
    base: str, age: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AccountAdapter, "is_open_for_signup", lambda *_: False)
    session = HeadlessSession(base)

    response = session.signup("new.player@example.org", **_age(age))

    assert response.status_code == 403
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_flagged_session_that_signs_in_gets_409(base: str) -> None:
    account = UserFactory.create(email=SIGNED_IN, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=SIGNED_IN, verified=True)
    session = HeadlessSession(base)
    assert session.signup(**_young()).status_code == 400
    login = session.post("auth/login", {"email": SIGNED_IN, "password": PASSWORD})
    assert login.status_code == 200

    response = session.signup("new.player@example.org")

    assert response.status_code == 409
