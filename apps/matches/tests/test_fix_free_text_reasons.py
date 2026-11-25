"""Only a moderator's reason, or the reason of the Event's Organizer
settling a dispute, reaches the append-only audit log (review finding L,
#26, ADR 0034).

An Audit Event can never be changed or deleted, so free text that could
hold a name is stored only from moderators and from the Organizer
settling a dispute. A dispute takes no reason. Anyone else who gives a
reason to void or edit, an Organizer member editing a match that is not
disputed included, is refused with fixed text, and nothing changes.
Made-up data only.
"""

from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus, Team
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import dispute_match, edit_match, void_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up free text."
REFUSAL = "Only a moderator, or an organizer settling a dispute, may give a reason."


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _events(match: Match, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action=action, target_object_id=str(match.pk))
    )


def _edit(match: Match, actor: Any, **extra: Any) -> Match:
    teams = {
        team: [p.player for p in match.participants.filter(team=team)] for team in Team
    }
    return edit_match(
        match,
        actor=actor,
        format=match.format,
        team_a=teams[Team.A],
        team_b=teams[Team.B],
        games=[(11, 3)],
        played_on=match.played_on,
        **extra,
    )


def test_a_dispute_takes_no_reason(pending: Match, players: list[Player]) -> None:
    with pytest.raises(TypeError):
        dispute_match(pending, actor=players[0].account, reason=REASON)  # type: ignore[call-arg]

    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING


def test_a_dispute_stores_no_reason(pending: Match, players: list[Player]) -> None:
    dispute_match(pending, actor=players[0].account)

    [event] = _events(pending, AuditAction.MATCH_DISPUTE)
    assert event.reason == ""


def test_a_non_moderator_void_with_a_reason_is_refused(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)

    with pytest.raises(ValueError, match=REFUSAL):
        void_match(match, actor=_member(event), reason=REASON)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.PENDING
    assert _events(match, AuditAction.MATCH_VOID) == []


def test_a_non_moderator_void_without_a_reason_is_allowed(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)

    void_match(match, actor=_member(event))

    [audit] = _events(match, AuditAction.MATCH_VOID)
    assert audit.reason == ""


def test_a_non_moderator_edit_with_a_reason_is_refused(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)
    count = Match.objects.count()

    with pytest.raises(ValueError, match=REFUSAL):
        _edit(match, _member(event), reason=REASON)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.PENDING
    assert Match.objects.count() == count


def test_a_moderator_reason_is_stored(event: Event, moderator: User) -> None:
    voided: Match = MatchFactory.create(event=event)
    edited: Match = MatchFactory.create(event=event)

    void_match(voided, actor=moderator, reason=REASON)
    _edit(edited, moderator, reason=REASON)

    assert _events(voided, AuditAction.MATCH_VOID)[0].reason == REASON
    assert _events(edited, AuditAction.MATCH_EDIT)[0].reason == REASON
