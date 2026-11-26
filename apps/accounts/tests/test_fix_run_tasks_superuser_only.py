"""Only a superuser runs periodic tasks from the admin (issue #101).

django_celery_beat's "Run selected tasks" action sends any registered
task at once. A staff Account with the periodic task permissions may
still enable, disable, and toggle tasks, but is neither offered the run
action nor allowed to run it. Made-up data only.
"""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import reverse
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:django_celery_beat_periodictask_changelist"
# Not a registered task, so a run reports it as not found.
TASK = "made.up.not_registered"


def _staff() -> "_User":
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.set(
        Permission.objects.filter(
            content_type__app_label="django_celery_beat",
            codename__in=["view_periodictask", "change_periodictask"],
        )
    )
    return account


def _task() -> PeriodicTask:
    schedule = IntervalSchedule.objects.create(every=1, period="days")
    task: PeriodicTask = PeriodicTask.objects.create(
        name="made-up-periodic", task=TASK, interval=schedule, enabled=False
    )
    return task


def _run(client: Client, task: PeriodicTask) -> list[str]:
    response = client.post(
        reverse(CHANGELIST),
        {"action": "run_tasks", "_selected_action": [str(task.pk)]},
        follow=True,
    )
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_staff_is_not_offered_the_run_action(client: Client) -> None:
    client.force_login(_staff())
    _task()

    page = client.get(reverse(CHANGELIST)).content.decode()

    assert 'value="run_tasks"' not in page
    assert 'value="enable_tasks"' in page


def test_staff_cannot_run_a_task(client: Client) -> None:
    client.force_login(_staff())
    task = _task()

    messages = _run(client, task)

    assert not any("not found" in message for message in messages)
    assert not AuditEvent.objects.filter(action=AuditAction.ADMIN_ACTION).exists()


def test_the_run_action_refuses_staff_even_when_called(rf: Any) -> None:
    task = _task()
    request = rf.post("/")
    request.user = _staff()
    request.session = {}
    request._messages = FallbackStorage(request)
    # django_celery_beat's admin defines it; the stubs know only ModelAdmin.
    run = admin.site._registry[PeriodicTask].run_tasks  # type: ignore[attr-defined]

    with pytest.raises(PermissionDenied):
        run(request, PeriodicTask.objects.filter(pk=task.pk))


def test_staff_can_still_enable_a_task(client: Client) -> None:
    client.force_login(_staff())
    task = _task()

    client.post(
        reverse(CHANGELIST),
        {"action": "enable_tasks", "_selected_action": [str(task.pk)]},
    )

    task.refresh_from_db()
    assert task.enabled


def test_a_superuser_runs_tasks_and_it_is_audited(client: Client) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    task = _task()

    messages = _run(client, task)

    assert any("not found" in message for message in messages)
    event = AuditEvent.objects.get(action=AuditAction.ADMIN_ACTION)
    assert event.after == {"action": "run_tasks"}
