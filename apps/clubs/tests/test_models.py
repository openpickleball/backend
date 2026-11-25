"""Clubs, Organizers, and the roles that link Accounts to them."""

import re

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import (
    Club,
    ClubOrganizerGrant,
    ClubRole,
    ClubRoleKind,
    MemberRole,
    Organizer,
    OrganizerMember,
)

pytestmark = pytest.mark.django_db

CLUB_CODE = re.compile(r"^C-[0-9A-Z]{4}-[0-9A-Z]{4}$")
ORGANIZER_CODE = re.compile(r"^G-[0-9A-Z]{4}-[0-9A-Z]{4}$")

# A made-up value that must never show up in a validation message.
MARKER = "zqxjmarker"


def _messages(error: ValidationError) -> str:
    return " ".join(
        message for messages in error.message_dict.values() for message in messages
    )


def test_club_gets_a_club_code() -> None:
    club = Club.objects.create(name="Made-up Paddle Club")

    assert CLUB_CODE.match(club.public_code)


def test_organizer_gets_an_organizer_code() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")

    assert ORGANIZER_CODE.match(organizer.public_code)


def test_str_is_the_open_code_not_the_name() -> None:
    club = Club.objects.create(name="Made-up Paddle Club")
    organizer = Organizer.objects.create(name="Made-up Person")

    assert str(club) == club.public_code
    assert str(organizer) == organizer.public_code


def test_new_organizer_is_unverified() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")

    assert organizer.verified_at is None
    assert organizer.verified_by is None
    assert organizer.is_verified is False


def test_is_verified_follows_verified_at() -> None:
    organizer = Organizer(name="Made-up Events", verified_at=timezone.now())

    assert organizer.is_verified is True


def test_verification_fields_are_not_editable() -> None:
    for name in ("verified_at", "verified_by"):
        assert Organizer._meta.get_field(name).editable is False


def test_verify_permission_is_declared() -> None:
    assert ("verify_organizer", "Can verify or unverify an organizer") in (
        Organizer._meta.permissions
    )


def test_database_refuses_verified_at_without_verified_by() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")

    with pytest.raises(IntegrityError), transaction.atomic():
        Organizer.objects.filter(pk=organizer.pk).update(verified_at=timezone.now())


def test_database_refuses_verified_by_without_verified_at() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    staff = UserFactory.create(is_staff=True)

    with pytest.raises(IntegrityError), transaction.atomic():
        Organizer.objects.filter(pk=organizer.pk).update(verified_by=staff)


def test_database_accepts_both_verification_fields_set() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    staff = UserFactory.create(is_staff=True)

    Organizer.objects.filter(pk=organizer.pk).update(
        verified_at=timezone.now(), verified_by=staff
    )

    organizer.refresh_from_db()
    assert organizer.is_verified is True


@pytest.mark.parametrize("model", [Club, Organizer])
@pytest.mark.parametrize("name", ["", "   "])
def test_database_refuses_blank_name(model: type[Club | Organizer], name: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.create(name=name)


@pytest.mark.parametrize("model", [Club, Organizer])
def test_full_clean_refuses_whitespace_only_name(
    model: type[Club | Organizer],
) -> None:
    record = model(name="   ")

    with pytest.raises(ValidationError) as error:
        record.full_clean()

    assert "name" in error.value.message_dict


def test_full_clean_strips_text_fields() -> None:
    club = Club(name="  Made-up Club  ", city=" Made-up City ", region=" MR ")

    club.full_clean()

    assert (club.name, club.city, club.region) == ("Made-up Club", "Made-up City", "MR")


def test_full_clean_strips_organizer_name() -> None:
    organizer = Organizer(name="  Made-up Events  ")

    organizer.full_clean()

    assert organizer.name == "Made-up Events"


@pytest.mark.parametrize("field", ["name", "city", "region"])
@pytest.mark.parametrize("bad", ["\x00", "\x07", "\ud800"])
def test_club_refuses_control_characters_without_echo(field: str, bad: str) -> None:
    values = {"name": "Made-up Club", field: f"{MARKER}{bad}"}
    club = Club(**values)

    with pytest.raises(ValidationError) as error:
        club.full_clean()

    assert field in error.value.message_dict
    assert MARKER not in _messages(error.value)


def test_organizer_refuses_control_characters_without_echo() -> None:
    organizer = Organizer(name=f"{MARKER}\x00")

    with pytest.raises(ValidationError) as error:
        organizer.full_clean()

    assert "name" in error.value.message_dict
    assert MARKER not in _messages(error.value)


def test_text_fields_refuse_non_text() -> None:
    club = Club(name=12345)

    with pytest.raises(ValidationError) as error:
        club.full_clean()

    assert "name" in error.value.message_dict


def test_organizer_member_links_account_with_role() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    account = UserFactory.create()

    member = OrganizerMember.objects.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )

    assert list(organizer.members.all()) == [member]
    assert list(account.organizer_memberships.all()) == [member]


def test_club_role_links_account_with_role() -> None:
    club = Club.objects.create(name="Made-up Club")
    account = UserFactory.create()

    role = ClubRole.objects.create(club=club, account=account, role=ClubRoleKind.ADMIN)

    assert list(club.roles.all()) == [role]
    assert list(account.club_roles.all()) == [role]


def test_organizer_member_is_unique_per_pair() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    account = UserFactory.create()
    OrganizerMember.objects.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerMember.objects.create(
            organizer=organizer, account=account, role=MemberRole.MATCH_MANAGER
        )


def test_club_role_is_unique_per_pair() -> None:
    club = Club.objects.create(name="Made-up Club")
    account = UserFactory.create()
    ClubRole.objects.create(club=club, account=account, role=ClubRoleKind.ADMIN)

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubRole.objects.create(club=club, account=account, role=ClubRoleKind.ADMIN)


def test_role_choices() -> None:
    assert MemberRole.values == ["admin", "match_manager"]


def test_club_role_choices_are_admin_only() -> None:
    # ADR 0021: a Club has only the admin role.
    assert ClubRoleKind.values == ["admin"]
    assert ClubRole._meta.get_field("role").choices == [("admin", "Admin")]


def test_club_role_match_manager_is_refused_by_full_clean() -> None:
    club = Club.objects.create(name="Made-up Club")
    record = ClubRole(club=club, account=UserFactory.create(), role="match_manager")

    with pytest.raises(ValidationError) as error:
        record.full_clean()

    assert "role" in error.value.message_dict


def test_database_refuses_club_role_match_manager() -> None:
    club = Club.objects.create(name="Made-up Club")
    account = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubRole.objects.create(club=club, account=account, role="match_manager")


@pytest.mark.parametrize("model", ["member", "club_role"])
def test_invalid_role_refused_without_echo(model: str) -> None:
    account = UserFactory.create()
    record: OrganizerMember | ClubRole
    if model == "member":
        organizer = Organizer.objects.create(name="Made-up Events")
        record = OrganizerMember(organizer=organizer, account=account, role=MARKER)
    else:
        club = Club.objects.create(name="Made-up Club")
        record = ClubRole(club=club, account=account, role=MARKER)

    with pytest.raises(ValidationError) as error:
        record.full_clean()

    assert "role" in error.value.message_dict
    assert MARKER not in _messages(error.value)


def test_database_refuses_invalid_member_role() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    account = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerMember.objects.create(
            organizer=organizer, account=account, role="owner"
        )


def test_database_refuses_invalid_club_role() -> None:
    club = Club.objects.create(name="Made-up Club")
    account = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubRole.objects.create(club=club, account=account, role="owner")


def test_role_str_has_code_and_role_not_email() -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    club = Club.objects.create(name="Made-up Club")
    account = UserFactory.create()
    member = OrganizerMember.objects.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    role = ClubRole.objects.create(club=club, account=account, role=ClubRoleKind.ADMIN)

    assert str(member) == f"{organizer.public_code} admin"
    assert str(role) == f"{club.public_code} admin"
    assert account.email not in str(member) + str(role)


def test_deleting_verifier_account_is_protected() -> None:
    staff = UserFactory.create(is_staff=True)
    organizer = Organizer.objects.create(name="Made-up Events")
    Organizer.objects.filter(pk=organizer.pk).update(
        verified_at=timezone.now(), verified_by=staff
    )

    with pytest.raises(ProtectedError):
        staff.delete()


# Club Organizer Grants (ADR 0021).


def test_club_organizer_grant_links_club_and_organizer() -> None:
    club = Club.objects.create(name="Made-up Club")
    organizer = Organizer.objects.create(name="Made-up Events")

    grant = ClubOrganizerGrant.objects.create(club=club, organizer=organizer)

    assert list(club.organizer_grants.all()) == [grant]
    assert list(organizer.club_grants.all()) == [grant]
    assert grant.created_at is not None


def test_club_organizer_grant_is_unique_per_pair() -> None:
    club = Club.objects.create(name="Made-up Club")
    organizer = Organizer.objects.create(name="Made-up Events")
    ClubOrganizerGrant.objects.create(club=club, organizer=organizer)

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubOrganizerGrant.objects.create(club=club, organizer=organizer)


def test_club_organizer_grant_str_has_codes_only() -> None:
    club = Club.objects.create(name="Made-up Club")
    organizer = Organizer.objects.create(name="Made-up Events")

    grant = ClubOrganizerGrant.objects.create(club=club, organizer=organizer)

    assert str(grant) == f"{club.public_code} {organizer.public_code}"
    assert "Made-up" not in str(grant)


def test_club_organizer_grant_cascades_with_club_and_organizer() -> None:
    club = Club.objects.create(name="Made-up Club")
    organizer = Organizer.objects.create(name="Made-up Events")
    by_club = ClubOrganizerGrant.objects.create(club=club, organizer=organizer)
    other_club = Club.objects.create(name="Made-up Other Club")
    by_organizer = ClubOrganizerGrant.objects.create(
        club=other_club, organizer=organizer
    )

    club.delete()
    assert not ClubOrganizerGrant.objects.filter(pk=by_club.pk).exists()
    organizer.delete()
    assert not ClubOrganizerGrant.objects.filter(pk=by_organizer.pk).exists()
