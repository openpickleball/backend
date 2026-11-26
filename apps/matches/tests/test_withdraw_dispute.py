"""A participant withdraws their own dispute (#267).

The withdrawal is a new ``withdraw`` row, never an edit or delete of the
dispute row. The match stays disputed while any dispute of the current
disputed period is open. When the last one is withdrawn, the match goes
back to the status it had when the period began: confirmed when it was
confirmed, else pending. Made-up data only.
"""

import pytest

from apps.accounts.models import User
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    ConfirmationDecision,
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import (
    WORKFLOW_GAMES,
    WORKFLOW_PLAYED_ON,
    SignalLog,
)
from apps.matches.workflow import (
    MatchStateError,
    dispute_match,
    resolve_dispute,
    void_match,
    withdraw_dispute,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _events(match: Match, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(
            action=action, target_object_id=str(match.pk)
        ).order_by("pk")
    )


def _event_match(event: Event, players: list[Player]) -> tuple[Match, User]:
    """A confirmed Event match and a member of its verified Organizer."""
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match
    return match, member


def _decisions(match: Match, player: Player) -> list[str]:
    return list(
        MatchConfirmation.objects.filter(match=match, player=player)
        .order_by("pk")
        .values_list("decision", flat=True)
    )


def test_withdrawing_the_only_dispute_confirms_again(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    signals.confirmed.clear()

    result = withdraw_dispute(confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.CONFIRMED
    assert Match.objects.counted().filter(pk=confirmed.pk).exists()
    [event] = _events(confirmed, AuditAction.MATCH_DISPUTE_WITHDRAW)
    assert event.before == {"status": MatchStatus.DISPUTED}
    assert event.after == {"status": MatchStatus.CONFIRMED}
    assert event.actor_user_id == players[0].account_id
    assert signals.confirmed == [confirmed.pk]


def test_the_withdrawal_is_a_new_row(confirmed: Match, players: list[Player]) -> None:
    dispute_match(confirmed, actor=players[0].account)

    withdraw_dispute(confirmed, actor=players[0].account)

    assert _decisions(confirmed, players[0]) == [
        ConfirmationDecision.CONFIRM,
        ConfirmationDecision.DISPUTE,
        ConfirmationDecision.WITHDRAW,
    ]


def test_withdrawing_a_dispute_of_a_pending_match_makes_it_pending(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(pending, actor=players[2].account)

    result = withdraw_dispute(pending, actor=players[2].account)

    assert result.status == MatchStatus.PENDING
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    [event] = _events(pending, AuditAction.MATCH_DISPUTE_WITHDRAW)
    assert event.after == {"status": MatchStatus.PENDING}
    assert signals.confirmed == []


def test_with_two_open_disputes_one_withdrawal_keeps_it_disputed(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    dispute_match(confirmed, actor=players[2].account)
    signals.confirmed.clear()
    signals.changed.clear()

    withdraw_dispute(confirmed, actor=players[0].account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED
    assert _events(confirmed, AuditAction.MATCH_DISPUTE_WITHDRAW) == []
    assert signals.confirmed == []
    assert signals.changed == []
    assert _decisions(confirmed, players[0])[-1] == ConfirmationDecision.WITHDRAW


def test_the_last_withdrawal_of_two_confirms_again(
    confirmed: Match, players: list[Player]
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    dispute_match(confirmed, actor=players[2].account)
    withdraw_dispute(confirmed, actor=players[0].account)

    withdraw_dispute(confirmed, actor=players[2].account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.CONFIRMED
    assert len(_events(confirmed, AuditAction.MATCH_DISPUTE_WITHDRAW)) == 1


def test_disputing_again_after_a_withdrawal_changes_nothing(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)
    signals.changed.clear()

    result = dispute_match(confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED
    assert len(_events(confirmed, AuditAction.MATCH_DISPUTE)) == 1
    assert signals.changed == []
    assert _decisions(confirmed, players[0]).count(ConfirmationDecision.DISPUTE) == 1


def test_a_repeat_withdrawal_changes_nothing(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    withdraw_dispute(confirmed, actor=players[0].account)
    signals.confirmed.clear()

    result = withdraw_dispute(confirmed, actor=players[0].account)

    assert result.status == MatchStatus.CONFIRMED
    assert len(_events(confirmed, AuditAction.MATCH_DISPUTE_WITHDRAW)) == 1
    assert signals.confirmed == []
    assert _decisions(confirmed, players[0]).count(ConfirmationDecision.WITHDRAW) == 1


def test_a_non_participant_is_refused(confirmed: Match, players: list[Player]) -> None:
    dispute_match(confirmed, actor=players[0].account)

    with pytest.raises(MatchActionDenied):
        withdraw_dispute(confirmed, actor=PlayerFactory.create().account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED


def test_an_unsaved_match_is_refused(players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        withdraw_dispute(Match(), actor=players[0].account)


def test_a_participant_who_never_disputed_is_refused(
    confirmed: Match, players: list[Player]
) -> None:
    dispute_match(confirmed, actor=players[0].account)

    with pytest.raises(MatchStateError):
        withdraw_dispute(confirmed, actor=players[1].account)

    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.DISPUTED
    assert _decisions(confirmed, players[1]) == []


def test_a_match_never_disputed_is_refused(
    confirmed: Match, players: list[Player]
) -> None:
    with pytest.raises(MatchStateError):
        withdraw_dispute(confirmed, actor=players[0].account)


def test_a_voided_match_is_refused(event: Event, players: list[Player]) -> None:
    match, member = _event_match(event, players)
    dispute_match(match, actor=players[0].account)
    void_match(match, actor=member)

    with pytest.raises(MatchStateError):
        withdraw_dispute(match, actor=players[0].account)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.VOIDED


def test_a_dispute_from_a_settled_period_cannot_be_withdrawn(
    event: Event, players: list[Player]
) -> None:
    match, member = _event_match(event, players)
    dispute_match(match, actor=players[0].account)
    resolve_dispute(
        match, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    dispute_match(match, actor=players[2].account)

    with pytest.raises(MatchStateError):
        withdraw_dispute(match, actor=players[0].account)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.DISPUTED


def test_a_redispute_after_a_resolution_then_withdrawn_confirms_again(
    event: Event, players: list[Player]
) -> None:
    match, member = _event_match(event, players)
    dispute_match(match, actor=players[0].account)
    resolve_dispute(
        match, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
    )
    dispute_match(match, actor=players[2].account)

    withdraw_dispute(match, actor=players[2].account)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED


def test_an_event_match_created_confirmed_returns_to_confirmed(
    event: Event, signals: SignalLog
) -> None:
    players = [PlayerFactory.create() for _ in range(4)]
    member = OrganizerMemberFactory.create(organizer=event.organizer).account
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match
    assert match.status == MatchStatus.CONFIRMED
    dispute_match(match, actor=players[3].account)
    assert signals.changes(match) == [MatchChange.DISPUTE]
    signals.confirmed.clear()

    withdraw_dispute(match, actor=players[3].account)

    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert Match.objects.counted().filter(pk=match.pk).exists()
    assert signals.confirmed == [match.pk]
