"""A repeated signup keeps the details held for the Account it made (#114).

The second, identical request takes the taken-address path. It must not
replace the hold bound to the new Account with one bound to none. Made-up
addresses on example.org only.
"""

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    ADULT_BIRTH,
    APP,
    BROWSER,
    SESSION_EMAIL,
    SIGNUP_GENDER_DIVISION,
    HeadlessSession,
)
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SignupDetails,
    take_signup_details,
)

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
DETAILS = SignupDetails(
    birth_year=ADULT_BIRTH["birth_year"],
    birth_month=ADULT_BIRTH["birth_month"],
    gender_division=SIGNUP_GENDER_DIVISION,
)


@pytest.mark.parametrize("base", CLIENTS)
def test_a_double_submit_keeps_the_hold(base: str) -> None:
    session = HeadlessSession(base)
    first = session.signup()

    again = session.signup()

    assert first.status_code == again.status_code == 401
    account = get_user_model()._default_manager.get_by_natural_key(SESSION_EMAIL)
    assert get_user_model()._default_manager.count() == 1
    assert take_signup_details(session.store(), account) == DETAILS


@pytest.mark.parametrize("base", CLIENTS)
def test_another_accounts_address_replaces_the_hold(base: str) -> None:
    other = UserFactory.create(email="other.player@example.org")
    EmailAddress.objects.create(user=other, email=other.email, verified=True)
    session = HeadlessSession(base)
    session.signup()

    response = session.signup(
        "other.player@example.org", birth_year=1980, gender_division="mens"
    )

    assert response.status_code == 401
    assert session.store()[SIGNUP_DETAILS_SESSION_KEY] == {
        "account": None,
        "year": 1980,
        "month": ADULT_BIRTH["birth_month"],
        "gender_division": "mens",
    }
