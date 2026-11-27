"""The expired Club invite sweep (#230). Made-up data only."""

from datetime import timedelta

import pytest
from celery.schedules import crontab
from django.conf import settings
from django.utils import timezone

from apps.clubs import tasks
from apps.clubs.models import ClubInvite
from apps.clubs.tests.factories import ClubInviteFactory, OrganizerInviteFactory
from config.celery import app as celery_app

pytestmark = pytest.mark.django_db


def test_the_sweep_deletes_expired_club_invites_only() -> None:
    expired = timezone.now() - timedelta(minutes=1)
    ClubInviteFactory.create(expires_at=expired)
    open_invite = ClubInviteFactory.create()
    OrganizerInviteFactory.create(expires_at=expired)

    assert tasks.sweep_club_invites() == 1
    assert list(ClubInvite.objects.values_list("pk", flat=True)) == [open_invite.pk]
    assert tasks.sweep_club_invites() == 0


def test_the_sweep_task_and_its_beat_entry() -> None:
    task = tasks.sweep_club_invites
    assert task.name == "apps.clubs.tasks.sweep_club_invites"
    assert celery_app.amqp.router.route({}, task.name)["queue"].name == "default"
    entry = settings.CELERY_BEAT_SCHEDULE["sweep-club-invites"]
    assert entry["task"] == task.name
    assert entry["options"] == {"expire_seconds": 3600}
    assert entry["schedule"] == crontab(hour=4, minute=10)
