"""Moderators do not review single matches (#273). Made-up data only.

A moderator voids matches only in bulk, from the match admin. They do not
resolve disputes, resolve flags, approve, edit, or void a single match, in
the admin or through the services. The match admin stays read-only and
lists every match by default.
"""

import inspect
from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.test import Client, RequestFactory
from django.urls import NoReverseMatch, reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches import workflow
from apps.matches.admin import MatchAdmin
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MODERATOR_GROUP, MatchActionDenied
from apps.matches.services import record_match
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    edit_match,
    resolve_dispute,
    void_match,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 5, 2)
GAMES = ((11, 7), (9, 11), (11, 4))
REASON = "Made-up moderator reason."
CHANGELIST = "admin:matches_match_changelist"


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


@pytest.fixture
def moderator() -> User:
    return _moderator()


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _event_match(players: list[Player]) -> Match:
    organizer = verified_organizer()
    event = EventFactory.create(organizer=organizer)
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match


def _self_reported(players: list[Player], games: tuple[tuple[int, int], ...]) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def _unchanged(match: Match, status: MatchStatus, events: int) -> None:
    match.refresh_from_db()
    assert match.status == status
    assert AuditEvent.objects.count() == events


def test_moderator_cannot_resolve_a_dispute(
    moderator: User, players: list[Player]
) -> None:
    match = dispute_match(_event_match(players), actor=players[2].account)
    events = AuditEvent.objects.count()
    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=moderator, outcome=MatchStatus.VOIDED, reason=REASON
        )
    _unchanged(match, MatchStatus.DISPUTED, events)


def test_moderator_cannot_void_a_single_match(
    moderator: User, players: list[Player]
) -> None:
    match = _event_match(players)
    events = AuditEvent.objects.count()
    with pytest.raises(MatchActionDenied):
        void_match(match, actor=moderator)
    _unchanged(match, MatchStatus.CONFIRMED, events)


def test_moderator_cannot_edit_a_match(moderator: User, players: list[Player]) -> None:
    match = _event_match(players)
    events = AuditEvent.objects.count()
    with pytest.raises(MatchActionDenied):
        edit_match(
            match,
            actor=moderator,
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 3), (11, 5)),
            played_on=PLAYED_ON,
        )
    _unchanged(match, MatchStatus.CONFIRMED, events)
    assert Match.objects.count() == 1


def test_workflow_has_no_moderator_review_services() -> None:
    assert not hasattr(workflow, "approve_match")
    assert not hasattr(workflow, "resolve_flags")
    assert "as_moderator" not in inspect.signature(resolve_dispute).parameters
    assert "as_moderator" not in inspect.signature(edit_match).parameters
    assert "reason" not in inspect.signature(void_match).parameters


def test_admin_actions_are_only_the_bulk_voids(moderator: User) -> None:
    from django.contrib import admin

    request = RequestFactory().get("/")
    request.user = moderator
    model_admin = MatchAdmin(Match, admin.site)
    actions = set(model_admin.get_actions(request))
    assert actions == {"void_batch_selected", "void_submitter_selected"}


def test_admin_has_no_edit_page() -> None:
    with pytest.raises(NoReverseMatch):
        reverse("admin:matches_match_edit", args=[1])


def test_admin_lists_every_match_by_default(
    client: Client, moderator: User, players: list[Player]
) -> None:
    # Other players, so the two are not near duplicates of each other.
    others = [PlayerFactory.create() for _ in range(4)]
    plain = _self_reported(players, GAMES)
    disputed = dispute_match(_self_reported(others, GAMES), actor=others[2].account)
    client.force_login(moderator)

    default = client.get(reverse(CHANGELIST))
    assert default.status_code == 200
    listed = {m.pk for m in default.context["cl"].result_list}
    assert listed == {plain.pk, disputed.pk}

    narrowed = client.get(reverse(CHANGELIST), {"held": "yes"})
    assert narrowed.status_code == 200
    assert {m.pk for m in narrowed.context["cl"].result_list} == {disputed.pk}


def test_flag_resolution_is_approved_only() -> None:
    assert FlagResolution.values == ["approved"]
