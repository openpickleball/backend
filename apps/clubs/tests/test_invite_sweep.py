"""The expired Organizer invite sweep (#229). Made-up data only."""

from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.utils import timezone

from apps.clubs import tasks
from apps.clubs.models import OrganizerInvite
from apps.clubs.tests.factories import OrganizerInviteFactory
from config.celery import app as celery_app

pytestmark = pytest.mark.django_db


def test_the_sweep_deletes_expired_invites_only() -> None:
    OrganizerInviteFactory.create(expires_at=timezone.now() - timedelta(minutes=1))
    open_invite = OrganizerInviteFactory.create()

    assert tasks.sweep_organizer_invites() == 1
    assert list(OrganizerInvite.objects.values_list("pk", flat=True)) == [
        open_invite.pk
    ]


def test_the_sweep_task_and_its_beat_entry() -> None:
    task = tasks.sweep_organizer_invites
    assert task.name == "apps.clubs.tasks.sweep_organizer_invites"
    assert celery_app.amqp.router.route({}, task.name)["queue"].name == "default"
    entry = settings.CELERY_BEAT_SCHEDULE["sweep-organizer-invites"]
    assert entry["task"] == task.name
    assert entry["options"] == {"expire_seconds": 3600}
    assert entry["schedule"] == crontab(hour=4, minute=0)
