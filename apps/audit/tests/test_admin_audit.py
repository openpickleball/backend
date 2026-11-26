"""Every change made through the admin is audited (issue #101).

Adds, changes, and deletes record ``admin.add``, ``admin.change``, and
``admin.delete`` with the acting Account, in the change's transaction.
Values are kept only for an explicit allowlist (an Account's permission
fields, a Group's name and permissions); ``admin.change`` also names
every changed field. Custom actions record one ``admin.action`` per
selected object, except those whose service records its own events.
Admins that already save through an audited service record nothing
more. No event holds an email, a name, a password hash, or a secret.
Made-up data only.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Club, ClubOrganizerGrant
from apps.clubs.tests.factories import ClubFactory, OrganizerFactory
from apps.events.models import Event

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ADMIN_ACTIONS = (
    AuditAction.ADMIN_ADD,
    AuditAction.ADMIN_CHANGE,
    AuditAction.ADMIN_DELETE,
    AuditAction.ADMIN_ACTION,
)


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def admin_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _admin_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action__in=ADMIN_ACTIONS).order_by("id"))


def _stored(event: AuditEvent) -> str:
    return json.dumps([event.before, event.after, event.reason])


def _account() -> "_User":
    """An Account whose join time the admin's form shows exactly (whole
    seconds), so saving the form leaves it unchanged."""
    account: _User = UserFactory.create()
    account.date_joined = account.date_joined.replace(microsecond=0)
    account.save()
    return account


def _user_data(account: "_User", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "email": account.email,
        "date_joined_0": account.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": account.date_joined.strftime("%H:%M:%S"),
        "last_login_0": "",
        "last_login_1": "",
    }
    if account.is_active:
        data["is_active"] = "on"
    data.update(extra)
    return data


def _group_url(name: str, *args: Any) -> str:
    return reverse(f"admin:auth_group_{name}", args=args)


# Adds


def test_adding_a_group_records_its_values(
    admin_client: Client, superuser: "_User"
) -> None:
    permission = Permission.objects.get(codename="view_auditevent")

    response = admin_client.post(
        _group_url("add"), {"name": "Made-up readers", "permissions": [permission.pk]}
    )

    assert response.status_code == 302
    group = Group.objects.get(name="Made-up readers")
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_ADD
    assert event.actor_user_id == superuser.pk
    assert event.target == group
    assert event.before is None
    assert event.after == {
        "values": {"name": "Made-up readers", "permissions": [permission.pk]}
    }


def test_adding_a_model_without_listed_values_records_none(
    admin_client: Client,
) -> None:
    response = admin_client.post(
        reverse("admin:clubs_club_add"),
        {
            "name": "Made-up Lakeside Club",
            "city": "",
            "region": "",
            "roles-TOTAL_FORMS": "0",
            "roles-INITIAL_FORMS": "0",
            "roles-MIN_NUM_FORMS": "0",
            "roles-MAX_NUM_FORMS": "1000",
        },
    )

    assert response.status_code == 302
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_ADD
    assert event.target == Club.objects.get()
    assert event.before is None
    assert event.after is None


def test_adding_an_account_records_no_email(admin_client: Client) -> None:
    response = admin_client.post(
        reverse("admin:accounts_user_add"),
        {
            "email": "made.up.newcomer@example.org",
            "usable_password": "true",
            "password1": "made-up-Passw0rd-x7k2",
            "password2": "made-up-Passw0rd-x7k2",
        },
    )

    assert response.status_code == 302
    added = get_user_model()._default_manager.get_by_natural_key(
        "made.up.newcomer@example.org"
    )
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_ADD
    assert event.target == added
    assert event.after == {
        "values": {
            "is_active": True,
            "is_staff": False,
            "is_superuser": False,
            "groups": [],
            "user_permissions": [],
        }
    }
    assert "made.up.newcomer" not in _stored(event)
    assert "pbkdf2" not in _stored(event)
    assert "md5" not in _stored(event)


# Changes


def test_changing_a_group_records_fields_and_values(
    admin_client: Client, superuser: "_User"
) -> None:
    group = Group.objects.create(name="Made-up before")

    admin_client.post(_group_url("change", group.pk), {"name": "Made-up after"})

    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_CHANGE
    assert event.actor_user_id == superuser.pk
    assert event.target == group
    assert event.before == {"values": {"name": "Made-up before", "permissions": []}}
    assert event.after == {
        "fields": ["name"],
        "values": {"name": "Made-up after", "permissions": []},
    }


def test_a_save_that_changes_nothing_records_nothing(admin_client: Client) -> None:
    group = Group.objects.create(name="Made-up same")

    response = admin_client.post(_group_url("change", group.pk), {"name": group.name})

    assert response.status_code == 302
    assert _admin_events() == []


def test_a_refused_change_records_nothing(admin_client: Client) -> None:
    group = Group.objects.create(name="Made-up same")

    response = admin_client.post(_group_url("change", group.pk), {"name": ""})

    assert response.status_code == 200
    assert _admin_events() == []


def test_an_email_change_records_the_field_name_only(admin_client: Client) -> None:
    account = _account()
    old = account.email

    response = admin_client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        _user_data(account, email="made.up.renamed@example.org"),
    )

    assert response.status_code == 302
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_CHANGE
    assert event.after is not None
    assert event.after["fields"] == ["email"]
    assert old not in _stored(event)
    assert "made.up.renamed" not in _stored(event)


def test_granting_staff_records_the_values(admin_client: Client) -> None:
    account = _account()

    admin_client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        _user_data(account, is_staff="on"),
    )

    [event] = _admin_events()
    assert event.before is not None
    assert event.after is not None
    assert event.before["values"]["is_staff"] is False
    assert event.after["values"]["is_staff"] is True
    assert event.after["fields"] == ["is_staff"]


def test_a_group_change_on_an_account_records_the_group_ids(
    admin_client: Client,
) -> None:
    account = _account()
    group = Group.objects.create(name="Made-up moderators")

    admin_client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        _user_data(account, groups=[group.pk]),
    )

    [event] = _admin_events()
    assert event.after is not None
    assert event.after["fields"] == ["groups"]
    assert event.after["values"]["groups"] == [group.pk]
    assert event.before is not None
    assert event.before["values"]["groups"] == []


def test_setting_a_password_records_the_field_name_only(
    admin_client: Client,
) -> None:
    account = UserFactory.create()
    stored_hash = account.password

    response = admin_client.post(
        reverse("admin:auth_user_password_change", args=[account.pk]),
        {
            "usable_password": "true",
            "password1": "made-up-Passw0rd-y8m3",
            "password2": "made-up-Passw0rd-y8m3",
        },
    )

    assert response.status_code == 302
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_CHANGE
    assert event.target == account
    assert event.after is not None
    assert event.after["fields"] == ["password"]
    account.refresh_from_db()
    assert stored_hash not in _stored(event)
    assert account.password not in _stored(event)
    assert "made-up-Passw0rd" not in _stored(event)


def test_an_organizer_name_change_records_the_name_field_only(
    admin_client: Client,
) -> None:
    organizer = OrganizerFactory.create(name="Made-up Old Name")

    response = admin_client.post(
        reverse("admin:clubs_organizer_change", args=[organizer.pk]),
        {
            "name": "Made-up New Name",
            "members-TOTAL_FORMS": "0",
            "members-INITIAL_FORMS": "0",
            "members-MIN_NUM_FORMS": "0",
            "members-MAX_NUM_FORMS": "1000",
        },
    )

    assert response.status_code == 302
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_CHANGE
    assert event.target == organizer
    assert event.before is None
    assert event.after == {"fields": ["name"]}
    assert "Made-up" not in _stored(event)


# Deletes


def test_deleting_a_group_records_its_values_before_it_goes(
    admin_client: Client, superuser: "_User"
) -> None:
    group = Group.objects.create(name="Made-up leaving")
    pk = group.pk

    response = admin_client.post(_group_url("delete", pk), {"post": "yes"})

    assert response.status_code == 302
    assert not Group.objects.filter(pk=pk).exists()
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_DELETE
    assert event.actor_user_id == superuser.pk
    assert event.target_object_id == str(pk)
    assert event.before == {"values": {"name": "Made-up leaving", "permissions": []}}
    assert event.after is None


def test_delete_selected_records_one_event_per_object(admin_client: Client) -> None:
    schedules = [
        IntervalSchedule.objects.create(every=n, period="minutes") for n in (3, 4)
    ]

    response = admin_client.post(
        reverse("admin:django_celery_beat_intervalschedule_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(s.pk) for s in schedules],
            "post": "yes",
        },
    )

    assert response.status_code == 302
    assert not IntervalSchedule.objects.exists()
    events = _admin_events()
    assert [e.action for e in events] == [AuditAction.ADMIN_DELETE] * 2
    assert sorted(e.target_object_id for e in events) == sorted(
        str(s.pk) for s in schedules
    )
    assert all(e.before is None for e in events)


def test_deleting_yourself_records_no_actor(client: Client) -> None:
    account = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    pk = account.pk

    response = client.post(
        reverse("admin:accounts_user_delete", args=[pk]), {"post": "yes"}
    )

    assert response.status_code == 302
    assert not get_user_model()._default_manager.filter(pk=pk).exists()
    [event] = _admin_events()
    assert event.action == AuditAction.ADMIN_DELETE
    assert event.target_object_id == str(pk)
    assert event.actor_user_id is None


# Actions


def _periodic_task(name: str) -> PeriodicTask:
    schedule = IntervalSchedule.objects.create(every=1, period="days")
    task: PeriodicTask = PeriodicTask.objects.create(
        name=name, task="made.up.task", interval=schedule, enabled=False
    )
    return task


def test_a_custom_action_records_one_event_per_object(
    admin_client: Client, superuser: "_User"
) -> None:
    tasks = [_periodic_task("made-up-a"), _periodic_task("made-up-b")]

    response = admin_client.post(
        reverse("admin:django_celery_beat_periodictask_changelist"),
        {"action": "enable_tasks", "_selected_action": [str(t.pk) for t in tasks]},
    )

    assert response.status_code == 302
    assert all(
        t.enabled for t in PeriodicTask.objects.filter(pk__in=[t.pk for t in tasks])
    )
    events = _admin_events()
    assert [e.action for e in events] == [AuditAction.ADMIN_ACTION] * 2
    assert sorted(e.target_object_id for e in events) == sorted(
        str(t.pk) for t in tasks
    )
    assert all(e.after == {"action": "enable_tasks"} for e in events)
    assert all(e.actor_user_id == superuser.pk for e in events)


def test_a_service_action_records_only_its_own_events(admin_client: Client) -> None:
    organizer = OrganizerFactory.create()

    admin_client.post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": "verify_selected",
            "_selected_action": [str(organizer.pk)],
            "reason": "Made-up checked identity.",
        },
    )

    organizer.refresh_from_db()
    assert organizer.is_verified
    assert _admin_events() == []
    assert AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).count() == 1


# Admins audited by their own services


def test_the_event_admin_records_only_event_events(admin_client: Client) -> None:
    organizer = OrganizerFactory.create()

    response = admin_client.post(
        reverse("admin:events_event_add"),
        {
            "name": "Made-up Fall Open",
            "kind": "tournament",
            "organizer": str(organizer.pk),
            "club": "",
            "start_date": "2026-09-05",
            "end_date": "2026-09-06",
        },
    )

    assert response.status_code == 302
    assert Event.objects.exists()
    assert _admin_events() == []
    assert AuditEvent.objects.filter(action=AuditAction.EVENT_CREATE).count() == 1


def test_the_grant_admin_records_only_access_events(admin_client: Client) -> None:
    club, organizer = ClubFactory.create(), OrganizerFactory.create()

    response = admin_client.post(
        reverse("admin:clubs_cluborganizergrant_add"),
        {"club": str(club.pk), "organizer": str(organizer.pk)},
    )

    assert response.status_code == 302
    assert ClubOrganizerGrant.objects.exists()
    assert _admin_events() == []
    assert (
        AuditEvent.objects.filter(action=AuditAction.CLUB_ORGANIZER_GRANT_ADD).count()
        == 1
    )


def test_every_registered_admin_is_audited() -> None:
    from django.contrib import admin

    from apps.audit.admin_audit import AdminAuditMixin

    service_audited = {Event, ClubOrganizerGrant}
    for model, model_admin in admin.site._registry.items():
        if model in service_audited or model._meta.label == (
            "partners.OrganizerApplicationGrant"
        ):
            assert not isinstance(model_admin, AdminAuditMixin), model
        else:
            assert isinstance(model_admin, AdminAuditMixin), model


@pytest.mark.parametrize(
    ("model_label", "action"),
    [
        ("matches.Match", "void_batch_selected"),
        ("matches.Match", "void_submitter_selected"),
        ("clubs.Organizer", "verify_selected"),
        ("clubs.Organizer", "unverify_selected"),
        ("clubs.Organizer", "delete_selected"),
        ("clubs.OrganizerReport", "close_selected"),
    ],
)
def test_service_actions_are_not_wrapped(
    rf: Any, superuser: "_User", model_label: str, action: str
) -> None:
    from django.apps import apps
    from django.contrib import admin

    model = apps.get_model(model_label)
    model_admin = admin.site._registry[model]
    request = rf.get("/")
    request.user = superuser

    entry = model_admin.get_actions(request)[action]
    assert entry is not None
    func = entry[0]

    # The function the admin class defines, not a wrapper: the bulk voids
    # must not run inside an outer transaction.
    assert func is getattr(type(model_admin), action)
