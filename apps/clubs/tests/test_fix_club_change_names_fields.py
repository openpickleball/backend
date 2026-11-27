"""``club.change`` names the changed fields, never their values (#230).

A self-created Club can be named after a person, and Audit Events are
append-only, so a value recorded there could never be erased. Like
``organizer.change``, the event holds only ``after.fields``. Made-up
data only.
"""

import json

import pytest
from allauth.account.models import EmailAddress

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.club_self_service import update_club
from apps.clubs.tests.factories import ClubFactory, ClubRoleFactory

pytestmark = pytest.mark.django_db


def test_club_change_records_field_names_only() -> None:
    club = ClubFactory.create(
        name="Made-up Pat Person Courts", city="Made-up Town", region="AA"
    )
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    ClubRoleFactory.create(club=club, account=admin)

    update_club(
        club.pk,
        actor=admin,
        name="Made-up Sam Person Courts",
        city="Made-up Town",
        region="BB",
    )

    [event] = AuditEvent.objects.filter(action=AuditAction.CLUB_CHANGE)
    assert event.before is None
    assert event.after == {"fields": ["name", "region"]}
    text = json.dumps([event.before, event.after, event.reason])
    for value in ("Pat Person", "Sam Person", "AA", "BB", "Made-up Town"):
        assert value not in text
