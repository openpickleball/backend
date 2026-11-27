"""The Moderator group and the permission split in the admin (#40, #273).

A data migration creates the group with exactly the permissions a
moderator needs. Staff in the group can run the two bulk void actions
(a moderator's only match actions) and verify an Organizer; staff
without it see no actions, and a forged action changes nothing. Made-up
data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, SubmissionBatch
from apps.matches.permissions import (
    MODERATOR_GROUP,
    MODERATOR_PERMISSIONS,
    is_moderator,
)
from apps.matches.services import record_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."
PLAYED_ON = date(2026, 4, 11)
MATCH_ACTIONS = ("void_batch_selected", "void_submitter_selected")
EXPECTED = {
    "matches.moderate_match",
    "matches.view_match",
    "matches.view_matchflag",
    "clubs.verify_organizer",
    "clubs.view_organizer",
    "clubs.view_organizerreport",
    "clubs.view_organizerverificationrequest",
    "players.remove_underage_player",
    "players.view_player",
}


def _codes(group: Group) -> set[str]:
    return {f"{p.content_type.app_label}.{p.codename}" for p in group.permissions.all()}


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _confirmed(
    players: list[Player],
    games: tuple[tuple[int, int], ...],
    event: Event,
    member: User,
    batch: SubmissionBatch | None = None,
) -> Match:
    """A match of ``event``, confirmed as its verified Organizer's."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        event=event,
        submitted_by_user=member,
        submission_batch=batch,
    ).match


def _in_group() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _staff_without_group() -> User:
    """Staff who may view matches and organizers, and nothing more."""
    account: User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label__in=["matches", "clubs"],
            codename__in=["view_match", "view_matchflag", "view_organizer"],
        )
    )
    return account


def _post_action(
    client: Client, url: str, action: str, pks: list[int], *, confirm: bool = False
) -> None:
    data: dict[str, object] = {
        "action": action,
        "_selected_action": [str(pk) for pk in pks],
        "reason": REASON,
    }
    if confirm:
        data["post"] = "yes"
    client.post(url, data)


def test_the_group_has_exactly_the_moderator_permissions() -> None:
    group = Group.objects.get(name=MODERATOR_GROUP)

    assert _codes(group) == EXPECTED
    assert set(MODERATOR_PERMISSIONS) == EXPECTED
    assert MODERATOR_GROUP == "Moderator"


def test_a_group_member_is_a_moderator() -> None:
    assert is_moderator(_in_group())
    assert not is_moderator(_staff_without_group())


def test_group_member_runs_every_moderator_action(
    client: Client, players: list[Player], event: Event, member: User
) -> None:
    moderator = _in_group()
    client.force_login(moderator)
    url = reverse("admin:matches_match_changelist")
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    first = _confirmed(players, ((11, 7), (11, 9)), event, member, batch)
    second = _confirmed(players, ((11, 3), (11, 2)), event, member)

    page = client.get(url)
    assert page.status_code == 200
    for action in MATCH_ACTIONS:
        assert action in page.content.decode()
    detail = client.get(reverse("admin:matches_match_change", args=[first.pk]))
    assert detail.status_code == 200

    _post_action(client, url, "void_batch_selected", [first.pk], confirm=True)
    assert Match.objects.get(pk=first.pk).status == MatchStatus.VOIDED
    assert Match.objects.get(pk=second.pk).status == MatchStatus.CONFIRMED

    _post_action(client, url, "void_submitter_selected", [second.pk], confirm=True)
    assert Match.objects.get(pk=second.pk).status == MatchStatus.VOIDED
    assert AuditEvent.objects.filter(action=AuditAction.MATCH_BULK_VOID).count() == 2

    organizer = OrganizerFactory.create()
    _post_action(
        client,
        reverse("admin:clubs_organizer_changelist"),
        "verify_selected",
        [organizer.pk],
    )
    organizer.refresh_from_db()
    assert organizer.verified_by_id == moderator.pk
    verify = AuditEvent.objects.get(
        action=AuditAction.ORGANIZER_VERIFY, actor_user=moderator
    )
    assert verify.reason == REASON


def test_staff_without_the_group_sees_no_actions(
    client: Client, players: list[Player], event: Event, member: User
) -> None:
    client.force_login(_staff_without_group())
    _confirmed(players, ((11, 7), (11, 9)), event, member)
    OrganizerFactory.create()

    matches_page = client.get(reverse("admin:matches_match_changelist"))
    organizers_page = client.get(reverse("admin:clubs_organizer_changelist"))

    assert matches_page.status_code == 200
    for action in MATCH_ACTIONS:
        assert action not in matches_page.content.decode()
    assert organizers_page.status_code == 200
    assert "verify_selected" not in organizers_page.content.decode()


def test_staff_without_the_group_forged_posts_change_nothing(
    client: Client, players: list[Player], event: Event, member: User
) -> None:
    client.force_login(_staff_without_group())
    url = reverse("admin:matches_match_changelist")
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    first = _confirmed(players, ((11, 7), (11, 9)), event, member, batch)
    second = _confirmed(players, ((11, 3), (11, 2)), event, member)
    organizer = OrganizerFactory.create()
    before = AuditEvent.objects.count()

    for action in MATCH_ACTIONS:
        for confirm in (False, True):
            _post_action(client, url, action, [first.pk], confirm=confirm)
            _post_action(client, url, action, [second.pk], confirm=confirm)
    _post_action(
        client,
        reverse("admin:clubs_organizer_changelist"),
        "verify_selected",
        [organizer.pk],
    )

    assert Match.objects.get(pk=first.pk).status == MatchStatus.CONFIRMED
    assert Match.objects.get(pk=second.pk).status == MatchStatus.CONFIRMED
    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert AuditEvent.objects.count() == before


def test_superuser_who_is_not_active_is_not_a_moderator() -> None:
    account: User = UserFactory.create(
        is_staff=True, is_superuser=True, is_active=False
    )

    assert not is_moderator(account)
