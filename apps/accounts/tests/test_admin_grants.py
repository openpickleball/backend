"""Only a superuser grants admin power (issue #101).

Staff status, superuser status, groups, and permissions decide what an
Account may do in the admin and the API. A staff Account that is not a
superuser sees those fields read-only, cannot change or delete a staff
or superuser Account, and cannot add, change, or delete a Group. The
admin's own password change records ``password.change``. Made-up data
only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import DEFAULT_PASSWORD, UserFactory
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

GRANT_FIELDS = ("is_staff", "is_superuser", "groups", "user_permissions")


def _staff(*codenames: str) -> "_User":
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(Permission.objects.filter(codename__in=codenames))
    return account


@pytest.fixture
def account_manager(client: Client) -> Client:
    """Staff that may view, add, change, and delete Accounts and Groups,
    but is not a superuser."""
    client.force_login(
        _staff(
            "view_user",
            "add_user",
            "change_user",
            "delete_user",
            "view_group",
            "add_group",
            "change_group",
            "delete_group",
        )
    )
    return client


@pytest.fixture
def superuser_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


def _plain_account(**fields: Any) -> "_User":
    account: _User = UserFactory.create(**fields)
    account.date_joined = account.date_joined.replace(microsecond=0)
    account.save()
    return account


def _user_data(account: "_User", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "email": account.email,
        "is_active": "on",
        "date_joined_0": account.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": account.date_joined.strftime("%H:%M:%S"),
        "last_login_0": "",
        "last_login_1": "",
    }
    data.update(extra)
    return data


def _change(client: Client, account: "_User", **extra: Any) -> Any:
    return client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        _user_data(account, **extra),
    )


# The Account admin


def test_grant_fields_are_read_only_for_staff(account_manager: Client) -> None:
    account = _plain_account()

    response = account_manager.get(
        reverse("admin:accounts_user_change", args=[account.pk])
    )

    form = response.context["adminform"].form
    assert response.status_code == 200
    for name in GRANT_FIELDS:
        assert name not in form.fields, name


def test_grant_fields_are_editable_for_a_superuser(superuser_client: Client) -> None:
    account = _plain_account()

    response = superuser_client.get(
        reverse("admin:accounts_user_change", args=[account.pk])
    )

    form = response.context["adminform"].form
    for name in GRANT_FIELDS:
        assert name in form.fields, name


def test_staff_cannot_grant_staff_or_groups(account_manager: Client) -> None:
    account = _plain_account()
    group = Group.objects.create(name="Made-up moderators")
    permission = Permission.objects.get(codename="delete_user")

    response = _change(
        account_manager,
        account,
        is_staff="on",
        is_superuser="on",
        groups=[group.pk],
        user_permissions=[permission.pk],
    )

    assert response.status_code == 302
    account.refresh_from_db()
    assert not account.is_staff
    assert not account.is_superuser
    assert not account.groups.exists()
    assert not account.user_permissions.exists()


def test_staff_can_still_change_a_players_account(account_manager: Client) -> None:
    account = _plain_account()

    response = _change(account_manager, account, is_active="")

    assert response.status_code == 302
    account.refresh_from_db()
    assert not account.is_active


@pytest.mark.parametrize("fields", [{"is_staff": True}, {"is_superuser": True}])
def test_staff_cannot_change_a_staff_or_superuser_account(
    account_manager: Client, fields: dict[str, bool]
) -> None:
    target = _plain_account(**fields)

    changed = _change(account_manager, target, is_active="")
    password = account_manager.post(
        reverse("admin:auth_user_password_change", args=[target.pk]),
        {
            "usable_password": "true",
            "password1": "made-up-Passw0rd-z9q4",
            "password2": "made-up-Passw0rd-z9q4",
        },
    )

    assert changed.status_code == 403
    assert password.status_code == 403
    target.refresh_from_db()
    assert target.is_active
    assert target.check_password(DEFAULT_PASSWORD)


@pytest.mark.parametrize("fields", [{"is_staff": True}, {"is_superuser": True}])
def test_staff_cannot_delete_a_staff_or_superuser_account(
    account_manager: Client, fields: dict[str, bool]
) -> None:
    target = _plain_account(**fields)

    response = account_manager.post(
        reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"}
    )

    assert response.status_code == 403
    assert get_user_model()._default_manager.filter(pk=target.pk).exists()


def test_a_bulk_delete_with_a_staff_account_deletes_nothing(
    account_manager: Client,
) -> None:
    player = _plain_account()
    staff = _plain_account(is_staff=True)

    response = account_manager.post(
        reverse("admin:accounts_user_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(player.pk), str(staff.pk)],
            "post": "yes",
        },
    )

    assert response.status_code == 403
    pks = {player.pk, staff.pk}
    assert get_user_model()._default_manager.filter(pk__in=pks).count() == 2
    assert not AuditEvent.objects.filter(action=AuditAction.ADMIN_DELETE).exists()


def test_the_bulk_delete_refuses_staff_even_past_the_page(rf: Any) -> None:
    from django.contrib import admin
    from django.core.exceptions import PermissionDenied

    actor = _staff("view_user", "delete_user")
    staff = _plain_account(is_staff=True)
    request = rf.post("/")
    request.user = actor
    model_admin = admin.site._registry[get_user_model()]

    with pytest.raises(PermissionDenied):
        model_admin.delete_queryset(
            request, get_user_model()._default_manager.filter(pk=staff.pk)
        )

    assert get_user_model()._default_manager.filter(pk=staff.pk).exists()


def test_a_superuser_can_change_and_delete_staff(superuser_client: Client) -> None:
    target = _plain_account(is_staff=True)

    changed = _change(superuser_client, target, is_staff="")
    deleted = superuser_client.post(
        reverse("admin:accounts_user_delete", args=[target.pk]), {"post": "yes"}
    )

    assert changed.status_code == 302
    assert deleted.status_code == 302
    assert not get_user_model()._default_manager.filter(pk=target.pk).exists()


def test_a_superusers_grant_is_audited_with_its_values(
    superuser_client: Client,
) -> None:
    account = _plain_account()
    group = Group.objects.create(name="Made-up moderators")

    _change(superuser_client, account, is_staff="on", groups=[group.pk])

    event = AuditEvent.objects.get(action=AuditAction.ADMIN_CHANGE)
    assert event.after is not None
    assert event.after["fields"] == ["groups", "is_staff"]
    assert event.after["values"]["is_staff"] is True
    assert event.after["values"]["groups"] == [group.pk]


# Groups


def test_staff_cannot_add_change_or_delete_a_group(account_manager: Client) -> None:
    group = Group.objects.create(name="Made-up moderators")

    added = account_manager.post(reverse("admin:auth_group_add"), {"name": "Other"})
    changed = account_manager.post(
        reverse("admin:auth_group_change", args=[group.pk]), {"name": "Renamed"}
    )
    deleted = account_manager.post(
        reverse("admin:auth_group_delete", args=[group.pk]), {"post": "yes"}
    )

    assert added.status_code == 403
    assert changed.status_code == 403
    assert deleted.status_code == 403
    assert Group.objects.filter(pk=group.pk, name="Made-up moderators").exists()
    assert not Group.objects.filter(name__in=["Other", "Renamed"]).exists()


def test_staff_can_still_view_groups(account_manager: Client) -> None:
    group = Group.objects.create(name="Made-up moderators")

    assert (
        account_manager.get(reverse("admin:auth_group_changelist")).status_code == 200
    )
    assert (
        account_manager.get(
            reverse("admin:auth_group_change", args=[group.pk])
        ).status_code
        == 200
    )


def test_a_superuser_manages_groups(superuser_client: Client) -> None:
    response = superuser_client.post(
        reverse("admin:auth_group_add"), {"name": "Made-up readers"}
    )

    assert response.status_code == 302
    assert Group.objects.filter(name="Made-up readers").exists()


# The admin's own password change


def _change_own_password(client: Client, new: str) -> Any:
    return client.post(
        reverse("admin:password_change"),
        {"old_password": DEFAULT_PASSWORD, "new_password1": new, "new_password2": new},
    )


def test_changing_your_own_password_is_audited(client: Client) -> None:
    staff = UserFactory.create(is_staff=True)
    client.force_login(staff)

    response = _change_own_password(client, "made-up-Passw0rd-w5e1")

    assert response.status_code == 302
    staff.refresh_from_db()
    assert staff.check_password("made-up-Passw0rd-w5e1")
    event = AuditEvent.objects.get(action=AuditAction.PASSWORD_CHANGE)
    assert event.actor_user_id == staff.pk
    assert event.target == staff
    assert "made-up-Passw0rd" not in str([event.before, event.after])
    assert staff.password not in str([event.before, event.after])


def test_a_failed_password_change_records_nothing(client: Client) -> None:
    staff = UserFactory.create(is_staff=True)
    client.force_login(staff)

    response = client.post(
        reverse("admin:password_change"),
        {
            "old_password": "not-the-password",
            "new_password1": "made-up-Passw0rd-w5e1",
            "new_password2": "made-up-Passw0rd-w5e1",
        },
    )

    assert response.status_code == 200
    staff.refresh_from_db()
    assert staff.check_password(DEFAULT_PASSWORD)
    assert not AuditEvent.objects.filter(action=AuditAction.PASSWORD_CHANGE).exists()
