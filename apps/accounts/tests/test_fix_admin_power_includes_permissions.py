"""Only a superuser changes an Account that holds any power (issue #101).

An Account in a group, or with a permission of its own, can do more than
a player (a moderator, say). A staff Account that is not a superuser
cannot change it, set its password, or delete it, alone or in a bulk
delete. Made-up data only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import DEFAULT_PASSWORD, UserFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def _manager() -> "_User":
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(
            codename__in=["view_user", "change_user", "delete_user"]
        )
    )
    return account


def _in_a_group() -> "_User":
    account: _User = UserFactory.create()
    account.groups.add(Group.objects.create(name="Made-up reviewers"))
    return account


def _with_a_permission() -> "_User":
    account: _User = UserFactory.create()
    account.user_permissions.add(Permission.objects.get(codename="view_auditevent"))
    return account


HOLDERS = [_in_a_group, _with_a_permission]


@pytest.fixture
def manager_client(client: Client) -> Client:
    client.force_login(_manager())
    return client


@pytest.mark.parametrize("make", HOLDERS)
def test_staff_cannot_change_an_account_that_holds_power(
    manager_client: Client, make: Any
) -> None:
    target = make()

    changed = manager_client.post(
        reverse("admin:accounts_user_change", args=[target.pk]),
        {"email": target.email},
    )
    password = manager_client.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {
            "usable_password": "true",
            "password1": "made-up-Passw0rd-c3d4",
            "password2": "made-up-Passw0rd-c3d4",
        },
    )

    assert changed.status_code == 403
    assert password.status_code == 403
    target.refresh_from_db()
    assert target.is_active
    assert target.check_password(DEFAULT_PASSWORD)


@pytest.mark.parametrize("make", HOLDERS)
def test_staff_cannot_delete_an_account_that_holds_power(
    manager_client: Client, make: Any
) -> None:
    target = make()

    response = manager_client.post(
        reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"}
    )

    assert response.status_code == 403
    assert get_user_model()._default_manager.filter(pk=target.pk).exists()


@pytest.mark.parametrize("make", HOLDERS)
def test_a_bulk_delete_with_such_an_account_deletes_nothing(
    manager_client: Client, make: Any
) -> None:
    player = UserFactory.create()
    target = make()

    response = manager_client.post(
        reverse("admin:accounts_user_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(player.pk), str(target.pk)],
            "post": "yes",
        },
    )

    assert response.status_code == 403
    pks = [player.pk, target.pk]
    assert get_user_model()._default_manager.filter(pk__in=pks).count() == 2


@pytest.mark.parametrize("make", HOLDERS)
def test_the_bulk_delete_refuses_such_an_account_past_the_page(
    rf: Any, make: Any
) -> None:
    from django.contrib import admin
    from django.core.exceptions import PermissionDenied

    request = rf.post("/")
    request.user = _manager()
    target = make()
    model_admin = admin.site._registry[get_user_model()]

    with pytest.raises(PermissionDenied):
        model_admin.delete_queryset(
            request, get_user_model()._default_manager.filter(pk=target.pk)
        )

    assert get_user_model()._default_manager.filter(pk=target.pk).exists()


@pytest.mark.parametrize("make", HOLDERS)
def test_a_superuser_can_delete_such_an_account(client: Client, make: Any) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    target = make()

    response = client.post(
        reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"}
    )

    assert response.status_code == 302
    assert not get_user_model()._default_manager.filter(pk=target.pk).exists()
