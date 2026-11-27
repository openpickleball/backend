"""The New Member Hold (#232, ADR 0057). Made-up data only.

``accept_invite`` sets ``hold_ends_at`` to now plus
``ORGANIZER_NEW_MEMBER_HOLD_HOURS`` when the Organizer is verified, and
none when it is not, or the setting is 0. A held admin has no admin
authority. The add event records the hold's end; an unheld member's
events keep their shape. The admin's member inline shows it read-only.
"""

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, Organizer, OrganizerMember
from apps.clubs.self_service import NotFound, accept_invite, invite_member, list_members
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.emails.models import QueuedEmail
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _account(*, mfa: bool = True) -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    if mfa:
        enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _admin_of(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _accepted(organizer: Organizer, role: str = MemberRole.MATCH_MANAGER) -> User:
    admin = _admin_of(organizer)
    invitee = _account()
    invite_member(organizer.pk, actor=admin, email=invitee.email, role=role)
    body = QueuedEmail.objects.filter(to=invitee.email).latest("pk").body
    key = body.split("/organizer/invites/")[1].split()[0]
    accept_invite(key, actor=invitee)
    return invitee


def test_joining_a_verified_organizer_holds_the_member_for_the_set_hours() -> None:
    organizer = verify(OrganizerFactory.create())
    before = timezone.now()

    invitee = _accepted(organizer)

    member = OrganizerMember.objects.get(account=invitee)
    assert member.hold_ends_at is not None
    assert (
        before + timedelta(hours=24)
        <= member.hold_ends_at
        <= timezone.now() + timedelta(hours=24)
    )
    [event] = AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_MEMBER_ADD, actor_user=invitee
    )
    assert event.after == {
        "organizer": organizer.public_code,
        "account": invitee.pk,
        "role": MemberRole.MATCH_MANAGER,
        "hold_ends_at": member.hold_ends_at.isoformat(),
    }


def test_the_hours_come_from_the_setting(settings: object) -> None:
    settings.ORGANIZER_NEW_MEMBER_HOLD_HOURS = 2  # type: ignore[attr-defined]
    organizer = verify(OrganizerFactory.create())

    invitee = _accepted(organizer)

    member = OrganizerMember.objects.get(account=invitee)
    assert member.hold_ends_at is not None
    assert member.hold_ends_at <= timezone.now() + timedelta(hours=2)


def test_zero_hours_turns_the_hold_off(settings: object) -> None:
    settings.ORGANIZER_NEW_MEMBER_HOLD_HOURS = 0  # type: ignore[attr-defined]
    organizer = verify(OrganizerFactory.create())

    invitee = _accepted(organizer)

    assert OrganizerMember.objects.get(account=invitee).hold_ends_at is None


def test_joining_an_unverified_organizer_sets_no_hold() -> None:
    organizer = OrganizerFactory.create()

    invitee = _accepted(organizer)

    assert OrganizerMember.objects.get(account=invitee).hold_ends_at is None
    [event] = AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_MEMBER_ADD, actor_user=invitee
    )
    assert event.after is not None
    assert "hold_ends_at" not in event.after


def test_a_held_admin_has_no_admin_authority() -> None:
    organizer = verify(OrganizerFactory.create())
    invitee = _accepted(organizer, role=MemberRole.ADMIN)

    with pytest.raises(NotFound):
        list_members(organizer.pk, actor=invitee)
    OrganizerMember.objects.filter(account=invitee).update(
        hold_ends_at=timezone.now() - timedelta(seconds=1)
    )
    assert len(list_members(organizer.pk, actor=invitee)) == 2


def test_an_invite_from_a_held_admin_cannot_be_accepted() -> None:
    organizer = verify(OrganizerFactory.create())
    held = _accepted(organizer, role=MemberRole.ADMIN)
    OrganizerMember.objects.filter(account=held).update(hold_ends_at=None)
    other = _account()
    invite_member(
        organizer.pk, actor=held, email=other.email, role=MemberRole.MATCH_MANAGER
    )
    body = QueuedEmail.objects.filter(to=other.email).latest("pk").body
    key = body.split("/organizer/invites/")[1].split()[0]
    OrganizerMember.objects.filter(account=held).update(
        hold_ends_at=timezone.now() + timedelta(hours=1)
    )

    with pytest.raises(NotFound):
        accept_invite(key, actor=other)


def test_the_admin_inline_shows_the_hold_read_only(client: Client) -> None:
    organizer = verify(OrganizerFactory.create())
    _accepted(organizer)
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    admin_client = client

    page = admin_client.get(
        reverse("admin:clubs_organizer_change", args=[organizer.pk])
    )

    assert page.status_code == 200
    formset = page.context["inline_admin_formsets"][0]
    assert "hold_ends_at" in formset.readonly_fields
