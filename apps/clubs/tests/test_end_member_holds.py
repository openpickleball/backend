"""A moderator ends New Member Holds early (#232, ADR 0057). Made-up data
only.

``end_member_holds`` lets an actor ``may_close`` allows (active, with
``clubs.verify_organizer``, not a member of the Organizer) end every
hold still in force on one Organizer, with a required reason. Each
member records one ``organizer_member.change`` event with the reason.
The Organizer admin's "End new member holds" action calls it.
"""

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, Organizer, OrganizerMember
from apps.clubs.services import (
    NotAllowedToEndHolds,
    end_member_holds,
)
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.matches.permissions import MODERATOR_GROUP

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REASON = "Checked with the made-up club by phone."


def _moderator() -> "_User":
    """A staff Account in the Moderator group, which holds
    ``clubs.verify_organizer`` and the Organizer admin's view."""
    account: _User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _held(organizer: Organizer) -> OrganizerMember:
    member: OrganizerMember = OrganizerMemberFactory.create(
        organizer=organizer, hold_ends_at=timezone.now() + timedelta(hours=10)
    )
    return member


def _changes() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action=AuditAction.ORGANIZER_MEMBER_CHANGE).order_by(
            "pk"
        )
    )


def test_a_moderator_ends_every_hold_in_force_with_one_event_each() -> None:
    organizer = verified_organizer()
    first, second = _held(organizer), _held(organizer)
    free = OrganizerMemberFactory.create(organizer=organizer)
    moderator = _moderator()

    ended = end_member_holds(organizer, actor=moderator, reason=f"  {REASON} ")

    assert ended == 2
    assert not OrganizerMember.objects.filter(hold_ends_at__isnull=False).exists()
    events = _changes()
    assert [event.target_object_id for event in events] == [
        str(first.pk),
        str(second.pk),
    ]
    for event in events:
        assert event.actor_user == moderator
        assert event.reason == REASON
        assert "hold_ends_at" in event.before
        assert "hold_ends_at" not in event.after
        assert event.after["role"] == MemberRole.MATCH_MANAGER
    assert free.hold_ends_at is None


def test_a_hold_already_over_is_left_alone() -> None:
    organizer = verified_organizer()
    member = OrganizerMemberFactory.create(
        organizer=organizer, hold_ends_at=timezone.now() - timedelta(hours=1)
    )

    assert end_member_holds(organizer, actor=_moderator(), reason=REASON) == 0
    member.refresh_from_db()
    assert member.hold_ends_at is not None
    assert _changes() == []


@pytest.mark.parametrize("reason", ["", "   ", "x" * 1001])
def test_a_blank_or_long_reason_changes_nothing(reason: str) -> None:
    organizer = verified_organizer()
    _held(organizer)

    with pytest.raises(ValueError, match="reason"):
        end_member_holds(organizer, actor=_moderator(), reason=reason)

    assert OrganizerMember.objects.filter(hold_ends_at__isnull=False).exists()
    assert _changes() == []


def test_an_actor_without_the_permission_is_refused() -> None:
    organizer = verified_organizer()
    _held(organizer)

    with pytest.raises(NotAllowedToEndHolds):
        end_member_holds(organizer, actor=UserFactory.create(), reason=REASON)

    assert _changes() == []


def test_a_member_of_the_organizer_is_refused() -> None:
    organizer = verified_organizer()
    _held(organizer)
    moderator = _moderator()
    OrganizerMemberFactory.create(organizer=organizer, account=moderator)

    with pytest.raises(NotAllowedToEndHolds):
        end_member_holds(organizer, actor=moderator, reason=REASON)

    assert _changes() == []


def _run(client: Client, organizer: Organizer, reason: str) -> list[str]:
    response = client.post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": "end_member_holds_selected",
            "_selected_action": [str(organizer.pk)],
            "reason": reason,
        },
        follow=True,
    )
    assert response.status_code == 200
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_the_admin_action_ends_the_holds(client: Client) -> None:
    organizer = verified_organizer()
    _held(organizer)
    moderator = _moderator()
    client.force_login(moderator)

    messages = _run(client, organizer, REASON)

    assert messages == ["1 new member hold(s) ended."]
    [event] = _changes()
    assert event.reason == REASON
    assert not AuditEvent.objects.filter(action=AuditAction.ADMIN_ACTION).exists()


def test_the_admin_action_needs_a_reason(client: Client) -> None:
    organizer = verified_organizer()
    _held(organizer)
    client.force_login(_moderator())

    messages = _run(client, organizer, " ")

    assert messages == ["Give a reason to end new member holds."]
    assert _changes() == []


def test_the_admin_action_shows_only_with_the_permission(client: Client) -> None:
    organizer = verified_organizer()
    staff: _User = UserFactory.create(is_staff=True)
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="view_organizer"
        )
    )
    client.force_login(staff)

    page = client.get(reverse("admin:clubs_organizer_changelist"))

    assert page.status_code == 200
    assert b"end_member_holds_selected" not in page.content
    assert organizer.pk
