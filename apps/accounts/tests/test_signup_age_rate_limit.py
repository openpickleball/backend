"""Every signup POST counts once against allauth's signup limit (#114).

allauth limits signup to 20 a minute per IP (ACCOUNT_RATE_LIMITS
default). The age screen's refusals and malformed ages count too, and the
429 comes before the screen. Made-up addresses on example.org only.
"""

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.players.age import age_limit_date
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
LIMIT = 20


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


@pytest.mark.parametrize("base", CLIENTS)
def test_under_13_refusals_reach_the_limit(base: str) -> None:
    statuses = [
        HeadlessSession(base).signup(f"kid{n}@example.org", **_young()).status_code
        for n in range(LIMIT)
    ]
    late = HeadlessSession(base)

    response = late.signup("late.kid@example.org", **_young())

    assert statuses == [400] * LIMIT
    assert response.status_code == 429
    # The 429 comes before the age screen: the session is not flagged.
    assert SIGNUP_REFUSED_SESSION_KEY not in late.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_flagged_session_reaches_the_limit(base: str) -> None:
    session = HeadlessSession(base)
    statuses = [session.signup(**_young()).status_code]
    statuses += [session.signup().status_code for _ in range(LIMIT - 1)]

    response = session.signup()

    assert statuses == [400] * LIMIT
    assert response.status_code == 429


@pytest.mark.parametrize("base", CLIENTS)
def test_malformed_ages_reach_the_limit(base: str) -> None:
    statuses = [
        HeadlessSession(base).signup(f"bad{n}@example.org", birth_month=13).status_code
        for n in range(LIMIT)
    ]

    response = HeadlessSession(base).signup("late.bad@example.org", birth_month=13)

    assert statuses == [400] * LIMIT
    assert response.status_code == 429


@pytest.mark.parametrize("base", CLIENTS)
def test_an_adult_signup_counts_once(base: str) -> None:
    statuses = [
        HeadlessSession(base).signup(f"adult{n}@example.org").status_code
        for n in range(LIMIT)
    ]

    response = HeadlessSession(base).signup("late.adult@example.org")

    assert statuses == [401] * LIMIT
    assert response.status_code == 429
    assert get_user_model()._default_manager.count() == LIMIT
