"""The Club Invite model (#230): one invite per Club and address, the key
stored only as its hash, and a ``__str__`` that names nothing. Made-up
data only.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.clubs.invites import InviteModel
from apps.clubs.models import ClubInvite
from apps.clubs.tests.factories import ClubFactory, ClubInviteFactory

pytestmark = pytest.mark.django_db


def test_a_club_invite_is_an_invite_model_with_no_admin_permissions() -> None:
    assert issubclass(ClubInvite, InviteModel)
    assert ClubInvite._meta.default_permissions == ()


def test_one_invite_per_club_and_address() -> None:
    club = ClubFactory.create()
    ClubInviteFactory.create(club=club, email="same.made-up@example.com")

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubInviteFactory.create(club=club, email="same.made-up@example.com")

    ClubInviteFactory.create(email="same.made-up@example.com")
    assert ClubInvite.objects.count() == 2


def test_str_is_the_row_id_only() -> None:
    invite = ClubInviteFactory.create(
        club=ClubFactory.create(name="Made-up Secret Courts"),
        email="hidden.made-up@example.com",
    )

    assert str(invite) == f"Club invite {invite.pk}"


def test_a_bad_club_link_has_fixed_text() -> None:
    invite = ClubInviteFactory.build(club_id="not-an-id")

    with pytest.raises(ValidationError) as caught:
        invite.clean_fields(exclude=["invited_by", "key_hash", "email", "expires_at"])

    assert "not-an-id" not in str(caught.value)
    assert "Choose an existing club." in str(caught.value)
