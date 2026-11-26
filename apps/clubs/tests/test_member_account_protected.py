"""An Account with an Organizer membership cannot be hard-deleted.

Deleting it would drop the only link from an Organizer that may be that
person to the person, and leave the name, which is personal data, with no
owner (ADR 0009, Retention). Account deletion removes the membership
first (ADR 0048).
"""

import pytest
from django.db.models import ProtectedError

from apps.clubs.models import ClubRole, OrganizerMember
from apps.clubs.tests.factories import ClubRoleFactory, OrganizerMemberFactory

pytestmark = pytest.mark.django_db


def test_deleting_account_with_membership_is_protected() -> None:
    member = OrganizerMemberFactory.create()

    with pytest.raises(ProtectedError):
        member.account.delete()

    assert OrganizerMember.objects.filter(pk=member.pk).exists()


def test_deleting_account_with_club_role_still_cascades() -> None:
    role = ClubRoleFactory.create()

    role.account.delete()

    assert not ClubRole.objects.filter(pk=role.pk).exists()
