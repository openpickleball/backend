"""Staff or superuser status goes only to an Account with MFA (issue #101).

Otherwise anyone with that Account's password signs in without a second
factor, enrolls their own TOTP, and opens the admin. The Account admin
refuses the grant with fixed text that names no Account. Made-up data
only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.accounts.admin import GRANT_NEEDS_MFA_MESSAGE
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture
def superuser_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


def _change(client: Client, account: "_User", **extra: Any) -> Any:
    data: dict[str, Any] = {"email": account.email, "is_active": "on", **extra}
    return client.post(reverse("admin:accounts_user_change", args=[account.pk]), data)


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_granting_power_to_an_account_without_mfa_is_refused(
    superuser_client: Client, field: str
) -> None:
    account = UserFactory.create()

    response = _change(superuser_client, account, **{field: "on"})

    content = response.content.decode()
    assert response.status_code == 200
    assert GRANT_NEEDS_MFA_MESSAGE in content
    assert account.email not in content.replace(f'value="{account.email}"', "")
    account.refresh_from_db()
    assert not account.is_staff
    assert not account.is_superuser
    assert not AuditEvent.objects.filter(action=AuditAction.ADMIN_CHANGE).exists()


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_granting_power_to_an_account_with_mfa_goes_through(
    superuser_client: Client, field: str
) -> None:
    account = UserFactory.create()
    enroll_totp(account)

    response = _change(superuser_client, account, **{field: "on"})

    assert response.status_code == 302
    account.refresh_from_db()
    assert getattr(account, field) is True


def test_an_existing_grant_without_mfa_is_not_refused_again(
    superuser_client: Client,
) -> None:
    account = UserFactory.create(is_staff=True, mfa=False)

    response = _change(superuser_client, account, is_staff="on", is_active="")

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.is_staff
    assert not account.is_active


def test_adding_an_account_never_grants_power(superuser_client: Client) -> None:
    response = superuser_client.post(
        reverse("admin:accounts_user_add"),
        {
            "email": "made.up.added@example.org",
            "usable_password": "true",
            "password1": "made-up-Passw0rd-a1b2",
            "password2": "made-up-Passw0rd-a1b2",
            "is_staff": "on",
            "is_superuser": "on",
        },
    )

    assert response.status_code == 302
    added = get_user_model()._default_manager.get_by_natural_key(
        "made.up.added@example.org"
    )
    assert not added.is_staff
    assert not added.is_superuser
