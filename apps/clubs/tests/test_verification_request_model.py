"""The Organizer Verification Request model (#229). Made-up data only."""

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.clubs.models import OrganizerVerificationRequest
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerVerificationRequestFactory,
)
from apps.matches.permissions import MODERATOR_GROUP, MODERATOR_PERMISSIONS

pytestmark = pytest.mark.django_db


def test_two_open_requests_on_one_organizer_are_refused_by_the_database() -> None:
    organizer = OrganizerFactory.create()
    OrganizerVerificationRequestFactory.create(organizer=organizer)

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerVerificationRequestFactory.create(organizer=organizer)


def test_a_closed_request_allows_a_new_open_one() -> None:
    organizer = OrganizerFactory.create()
    OrganizerVerificationRequestFactory.create(
        organizer=organizer, closed_at=timezone.now()
    )

    OrganizerVerificationRequestFactory.create(organizer=organizer)

    assert OrganizerVerificationRequest.objects.filter(organizer=organizer).count() == 2


def test_an_organizer_with_a_request_cannot_be_deleted() -> None:
    request = OrganizerVerificationRequestFactory.create()

    with pytest.raises(ProtectedError):
        request.organizer.delete()


def test_str_names_the_row_only() -> None:
    request = OrganizerVerificationRequestFactory.create(
        organizer=OrganizerFactory.create(name="Made-up Person Name")
    )

    assert str(request) == f"Organizer verification request {request.pk}"


def test_the_moderator_group_may_view_requests() -> None:
    group = Group.objects.get(name=MODERATOR_GROUP)
    codes = {
        f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()
    }

    assert "clubs.view_organizerverificationrequest" in codes
    assert "clubs.view_organizerverificationrequest" in MODERATOR_PERMISSIONS
