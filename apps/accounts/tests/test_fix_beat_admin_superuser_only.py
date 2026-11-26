"""Only a superuser writes periodic tasks and schedules (issue #101).

DatabaseScheduler sends whatever the PeriodicTask table says. A staff
Account allowed to add or change a task could name any registered task
with chosen arguments and an every-second interval or a one-off clocked
time, and changing a schedule changes when the tasks using it run. So
OPEN's django_celery_beat admins allow add, change, and delete, and
every PeriodicTask action, to superusers only. Reading stays with
Django's view permissions. Made-up data only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_celery_beat.models import (
    ClockedSchedule,
    CrontabSchedule,
    IntervalSchedule,
    PeriodicTask,
    SolarSchedule,
)

from apps.accounts.tests.factories import UserFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ACTIONS = ("enable_tasks", "disable_tasks", "toggle_tasks", "run_tasks")


def _interval() -> IntervalSchedule:
    schedule: IntervalSchedule = IntervalSchedule.objects.create(every=1, period="days")
    return schedule


def _objects() -> dict[str, Model]:
    return {
        "periodictask": PeriodicTask.objects.create(
            name="made-up-periodic", task="made.up.task", interval=_interval()
        ),
        "intervalschedule": IntervalSchedule.objects.create(every=2, period="days"),
        "crontabschedule": CrontabSchedule.objects.create(minute="5"),
        "solarschedule": SolarSchedule.objects.create(
            event="sunrise", latitude=0, longitude=0
        ),
        "clockedschedule": ClockedSchedule.objects.create(clocked_time=timezone.now()),
    }


def _beat_staff() -> "_User":
    """Staff with every django_celery_beat permission, not a superuser."""
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(content_type__app_label="django_celery_beat")
    )
    return account


@pytest.fixture
def staff_client(client: Client) -> Client:
    client.force_login(_beat_staff())
    return client


@pytest.fixture
def superuser_client(client: Client) -> Client:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


def _url(model: str, view: str, *args: Any) -> str:
    return reverse(f"admin:django_celery_beat_{model}_{view}", args=args)


def test_staff_cannot_add_change_or_delete(staff_client: Client) -> None:
    for model, obj in _objects().items():
        assert staff_client.get(_url(model, "add")).status_code == 403, model
        assert staff_client.post(_url(model, "add"), {}).status_code == 403, model
        assert (
            staff_client.post(_url(model, "change", obj.pk), {}).status_code == 403
        ), model
        assert (
            staff_client.post(
                _url(model, "delete", obj.pk), {"post": "yes"}
            ).status_code
            == 403
        ), model
        assert type(obj)._default_manager.filter(pk=obj.pk).exists(), model


def test_staff_can_still_read(staff_client: Client) -> None:
    for model, obj in _objects().items():
        assert staff_client.get(_url(model, "changelist")).status_code == 200, model
        assert staff_client.get(_url(model, "change", obj.pk)).status_code == 200, model


def test_staff_is_offered_no_task_action(staff_client: Client) -> None:
    _objects()

    page = staff_client.get(_url("periodictask", "changelist")).content.decode()

    for action in (*ACTIONS, "delete_selected"):
        assert f'value="{action}"' not in page, action


@pytest.mark.parametrize("action", ACTIONS)
def test_each_task_action_refuses_staff_when_called(rf: Any, action: str) -> None:
    task = PeriodicTask.objects.get(pk=_objects()["periodictask"].pk)
    request = rf.post("/")
    request.user = _beat_staff()
    request.session = {}
    request._messages = FallbackStorage(request)
    model_admin = admin.site._registry[PeriodicTask]

    with pytest.raises(PermissionDenied):
        getattr(model_admin, action)(request, PeriodicTask.objects.filter(pk=task.pk))

    task.refresh_from_db()
    assert task.enabled


def test_a_superuser_adds_and_is_offered_the_actions(superuser_client: Client) -> None:
    objects = _objects()

    for model in objects:
        assert superuser_client.get(_url(model, "add")).status_code == 200, model
    page = superuser_client.get(_url("periodictask", "changelist")).content.decode()
    for action in ACTIONS:
        assert f'value="{action}"' in page, action
    added = superuser_client.post(
        _url("intervalschedule", "add"),
        {
            "every": "9",
            "period": "hours",
            "periodictask_set-TOTAL_FORMS": "0",
            "periodictask_set-INITIAL_FORMS": "0",
            "periodictask_set-MIN_NUM_FORMS": "0",
            "periodictask_set-MAX_NUM_FORMS": "1000",
        },
    )
    assert added.status_code == 302
    assert IntervalSchedule.objects.filter(every=9, period="hours").exists()
