"""Factories for Clubs, Organizers, and their roles. Made-up data only.

``OrganizerMemberFactory`` enrolls its Account in TOTP (ADR 0043), since
Organizer authority needs MFA, unless the Account already has a method
or the test passes ``mfa=False``.
"""

from datetime import timedelta
from typing import Any

from django.contrib.auth.models import Permission
from django.utils import timezone
from factory.declarations import LazyFunction, PostGeneration, Sequence, SubFactory
from factory.django import DjangoModelFactory
from faker import Faker

from apps.accounts.mfa import has_mfa
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.invites import hash_invite_key, new_invite_key
from apps.clubs.models import (
    Club,
    ClubInvite,
    ClubOrganizerGrant,
    ClubRole,
    ClubRoleKind,
    MemberRole,
    Organizer,
    OrganizerInvite,
    OrganizerMember,
    OrganizerVerificationRequest,
)
from apps.clubs.services import verify_organizer

_faker = Faker()


def _company_name() -> str:
    return str(_faker.company())[:200]


class ClubFactory(DjangoModelFactory[Club]):
    """A saved Club, created through ``objects.create`` so it gets a code."""

    class Meta:
        model = Club

    # factory_boy's declarations carry no type hints.
    name = LazyFunction(_company_name)  # type: ignore[no-untyped-call]
    city = LazyFunction(lambda: _faker.city()[:100])  # type: ignore[no-untyped-call]
    region = LazyFunction(lambda: _faker.state()[:100])  # type: ignore[no-untyped-call]


class OrganizerFactory(DjangoModelFactory[Organizer]):
    """A saved, unverified Organizer. Verify it through the services."""

    class Meta:
        model = Organizer

    name = LazyFunction(_company_name)  # type: ignore[no-untyped-call]


def _enroll_member(
    member: OrganizerMember, create: bool, extracted: object, **kwargs: Any
) -> None:
    """The ``mfa`` flag: enroll the member's Account unless told not to."""
    if create and extracted is not False and not has_mfa(member.account):
        enroll_totp(member.account)


class OrganizerMemberFactory(DjangoModelFactory[OrganizerMember]):
    """An Account acting for an Organizer, as a match manager by default,
    enrolled in TOTP (``mfa=False`` leaves it without a method)."""

    class Meta:
        model = OrganizerMember
        # ``mfa`` writes its own rows; the member needs no second save.
        skip_postgeneration_save = True

    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]
    account = SubFactory(UserFactory)  # type: ignore[no-untyped-call]
    role = MemberRole.MATCH_MANAGER
    mfa = PostGeneration(_enroll_member)  # type: ignore[no-untyped-call]


class ClubRoleFactory(DjangoModelFactory[ClubRole]):
    """An Account's admin role at a Club, the only Club Role."""

    class Meta:
        model = ClubRole

    club = SubFactory(ClubFactory)  # type: ignore[no-untyped-call]
    account = SubFactory(UserFactory)  # type: ignore[no-untyped-call]
    role = ClubRoleKind.ADMIN


class ClubOrganizerGrantFactory(DjangoModelFactory[ClubOrganizerGrant]):
    """A Club's consent for an Organizer to name it."""

    class Meta:
        model = ClubOrganizerGrant

    club = SubFactory(ClubFactory)  # type: ignore[no-untyped-call]
    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]


class OrganizerInviteFactory(DjangoModelFactory[OrganizerInvite]):
    """An open invite to a made-up address, as a match manager.

    The key is random and not kept; pass ``key_hash=hash_invite_key(key)``
    to know it.
    """

    class Meta:
        model = OrganizerInvite

    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]
    invited_by = SubFactory(UserFactory)  # type: ignore[no-untyped-call]
    email = Sequence(lambda n: f"invitee{n}@example.com")  # type: ignore[no-untyped-call]
    key_hash = LazyFunction(lambda: hash_invite_key(new_invite_key()))  # type: ignore[no-untyped-call]
    role = MemberRole.MATCH_MANAGER
    expires_at = LazyFunction(lambda: timezone.now() + timedelta(days=7))  # type: ignore[no-untyped-call]


class ClubInviteFactory(DjangoModelFactory[ClubInvite]):
    """An open invite to a made-up address to be a Club admin.

    The key is random and not kept; pass ``key_hash=hash_invite_key(key)``
    to know it.
    """

    class Meta:
        model = ClubInvite

    club = SubFactory(ClubFactory)  # type: ignore[no-untyped-call]
    invited_by = SubFactory(UserFactory)  # type: ignore[no-untyped-call]
    email = Sequence(lambda n: f"club.invitee{n}@example.com")  # type: ignore[no-untyped-call]
    key_hash = LazyFunction(lambda: hash_invite_key(new_invite_key()))  # type: ignore[no-untyped-call]
    expires_at = LazyFunction(lambda: timezone.now() + timedelta(days=7))  # type: ignore[no-untyped-call]


class OrganizerVerificationRequestFactory(
    DjangoModelFactory[OrganizerVerificationRequest]
):
    """An open verification request with a made-up statement."""

    class Meta:
        model = OrganizerVerificationRequest

    organizer = SubFactory(OrganizerFactory)  # type: ignore[no-untyped-call]
    statement = "Made-up weekly ladder at a community center."


def verify(organizer: Organizer) -> Organizer:
    """Verify ``organizer`` through ``verify_organizer``, the real path.

    The verifier is a new Account with ``clubs.verify_organizer`` that is
    not a member of the Organizer. Records one Audit Event.
    """
    verifier = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    return verify_organizer(organizer, actor=verifier)


def verified_organizer(**kwargs: Any) -> Organizer:
    """A new Organizer, verified through the services."""
    organizer: Organizer = OrganizerFactory.create(**kwargs)
    return verify(organizer)
