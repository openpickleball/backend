"""Whether an Account's login address is verified (#229).

Only the ``EmailAddress`` row for ``User.email`` counts: a verified old
address, or another pending one, does not. Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.addresses import has_verified_login_address
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_a_verified_login_address_counts() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )

    assert has_verified_login_address(account) is True


def test_an_unverified_login_address_does_not_count() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=False, primary=True
    )

    assert has_verified_login_address(account) is False


def test_no_address_row_does_not_count() -> None:
    assert has_verified_login_address(UserFactory.create()) is False


def test_another_verified_address_does_not_count() -> None:
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email="old.made-up@example.com", verified=True
    )
    EmailAddress.objects.create(user=account, email=account.email, verified=False)

    assert has_verified_login_address(account) is False


def test_an_unsaved_account_does_not_count() -> None:
    assert has_verified_login_address(UserFactory.build()) is False
