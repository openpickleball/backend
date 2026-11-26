"""The Account and Group admins refuse a tampered many-to-many value
without echoing it (issue #162).

Django's ``ModelMultipleChoiceField`` messages name the submitted value
("Select a valid choice. <value> is not one of the available choices."
and "'<value>' is not a valid value."). Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from config.admin_autocomplete import MULTIPLE_CHOICE_ERRORS

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

BAD_VALUES = ["987654321", "zqxjmarker"]
FIXED = set(MULTIPLE_CHOICE_ERRORS.values())


@pytest.fixture
def admin_client(client: Client) -> Client:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    return client


@pytest.mark.parametrize("field", ["groups", "user_permissions"])
@pytest.mark.parametrize("bad", BAD_VALUES)
def test_account_admin_refuses_tampered_value_without_echo(
    admin_client: Client, field: str, bad: str
) -> None:
    account = UserFactory.create()

    response = admin_client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        {"email": account.email, "is_active": "on", field: [bad]},
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert field in errors
    assert set(errors[field]) <= FIXED
    assert bad not in response.content.decode()
    assert not getattr(account, field).exists()


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_group_admin_refuses_tampered_permission_without_echo(
    admin_client: Client, bad: str
) -> None:
    response = admin_client.post(
        reverse("admin:auth_group_add"),
        {"name": "Made-up group", "permissions": [bad]},
    )

    assert response.status_code == 200
    errors = response.context["adminform"].form.errors
    assert "permissions" in errors
    assert set(errors["permissions"]) <= FIXED
    assert bad not in response.content.decode()
    assert not Group.objects.filter(name="Made-up group").exists()
