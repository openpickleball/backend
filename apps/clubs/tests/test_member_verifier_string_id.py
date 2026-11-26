"""The verifier cannot become a member through a string Account id.

Issue #162 review. The verifier check compared the Organizer's stored
``verified_by_id`` (an int) with the instance's raw ``account_id``, so a
string id such as ``"42"`` never matched, and the Account that verified
an Organizer could join it through ``clean()`` and ``save()``. Made-up
data only.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.clubs.models import MemberRole, OrganizerMember
from apps.clubs.tests.factories import verified_organizer

pytestmark = pytest.mark.django_db


def _verifier_as_member() -> OrganizerMember:
    organizer = verified_organizer()
    assert organizer.verified_by_id is not None
    return OrganizerMember(
        organizer=organizer,
        account_id=str(organizer.verified_by_id),
        role=MemberRole.ADMIN,
    )


def test_clean_refuses_verifier_by_string_id() -> None:
    with pytest.raises(ValidationError) as excinfo:
        _verifier_as_member().full_clean()

    assert "account" in excinfo.value.message_dict


def test_save_refuses_verifier_by_string_id() -> None:
    member = _verifier_as_member()

    with pytest.raises(ValidationError):
        member.save()

    assert not OrganizerMember.objects.exists()
