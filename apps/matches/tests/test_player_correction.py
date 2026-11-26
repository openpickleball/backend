"""A player corrects a pending or disputed self-reported match (#267).

A participant of a match with no Event and no Club corrects it through
``edit_match(as_player=True)`` with the same two teams. The old match is voided, and the
correction is pending with the corrector's confirmation, so it counts
only once a player on the other team confirms it. Made-up data only.
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
from apps.matches.permissions import (
    MatchActionDenied,
    can_correct_as_player,
    corrects_as_player,
)
from apps.matches.services import record_match
from apps.matches.tests.conftest import (
    WORKFLOW_GAMES,
    WORKFLOW_PLAYED_ON,
    SignalLog,
)
from apps.matches.workflow import (
    MatchStateError,
    ReasonRefused,
    confirm_match,
    dispute_match,
    edit_match,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

NEW_GAMES = [(11, 8), (11, 6)]


def _correct(
    match: Match,
    actor: object,
    team_a: list[Player],
    team_b: list[Player],
    reason: str = "",
) -> Match:
    return edit_match(
        match,
        actor=actor,  # type: ignore[arg-type]
        reason=reason,
        format=MatchFormat.DOUBLES,
        team_a=team_a,
        team_b=team_b,
        games=NEW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        as_player=True,
    )


def _decisions(match: Match) -> list[tuple[int, str]]:
    return list(
        MatchConfirmation.objects.filter(match=match)
        .order_by("pk")
        .values_list("player_id", "decision")
    )


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def test_a_player_corrects_a_pending_match(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    corrected = _correct(pending, players[2].account, players[:2], players[2:])

    assert _status(pending) == MatchStatus.VOIDED
    assert corrected.status == MatchStatus.PENDING
    assert corrected.corrects_id == pending.pk
    assert _decisions(corrected) == [(players[2].pk, ConfirmationDecision.CONFIRM)]
    assert not Match.objects.counted().filter(pk=corrected.pk).exists()
    assert signals.confirmed == []
    assert signals.changed == []
    [event] = AuditEvent.objects.filter(
        action=AuditAction.MATCH_EDIT, target_object_id=str(pending.pk)
    )
    assert event.before == {"status": MatchStatus.PENDING}
    assert event.after == {
        "status": MatchStatus.VOIDED,
        "corrected_by": corrected.public_code,
        "correction_status": MatchStatus.PENDING,
    }
    assert event.actor_user_id == players[2].account_id


def test_the_correction_counts_once_the_other_team_confirms(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    corrected = _correct(pending, players[2].account, players[:2], players[2:])

    confirm_match(corrected, actor=players[3].account)
    assert _status(corrected) == MatchStatus.PENDING

    confirm_match(corrected, actor=players[0].account)

    assert _status(corrected) == MatchStatus.CONFIRMED
    assert Match.objects.counted().filter(pk=corrected.pk).exists()
    assert signals.confirmed == [corrected.pk]


def test_a_player_corrects_a_disputed_match(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[0].account)
    signals.changed.clear()

    corrected = _correct(confirmed, players[0].account, players[:2], players[2:])

    assert _status(confirmed) == MatchStatus.VOIDED
    assert corrected.status == MatchStatus.PENDING
    assert _decisions(corrected) == [(players[0].pk, ConfirmationDecision.CONFIRM)]
    assert not Match.objects.counted().filter(pk=corrected.pk).exists()
    assert signals.confirmed == []
    assert signals.changed == []


def test_swapped_team_order_is_accepted(pending: Match, players: list[Player]) -> None:
    corrected = _correct(
        pending, players[0].account, players[2:], list(reversed(players[:2]))
    )

    assert corrected.status == MatchStatus.PENDING
    assert _status(pending) == MatchStatus.VOIDED


@pytest.mark.parametrize(
    "teams",
    [
        "new_player",
        "moved_player",
    ],
)
def test_changed_teams_are_refused(
    pending: Match, players: list[Player], teams: str
) -> None:
    if teams == "new_player":
        team_a, team_b = [players[0], PlayerFactory.create()], players[2:]
    else:
        team_a, team_b = [players[0], players[2]], [players[1], players[3]]

    with pytest.raises(MatchActionDenied):
        _correct(pending, players[0].account, team_a, team_b)

    assert _status(pending) == MatchStatus.PENDING
    assert not Match.objects.filter(corrects=pending).exists()


def test_a_confirmed_match_is_refused(confirmed: Match, players: list[Player]) -> None:
    with pytest.raises(MatchStateError):
        _correct(confirmed, players[0].account, players[:2], players[2:])

    assert _status(confirmed) == MatchStatus.CONFIRMED


def test_a_voided_match_is_refused(pending: Match, players: list[Player]) -> None:
    # A first correction voids the old match.
    _correct(pending, players[0].account, players[:2], players[2:])
    assert _status(pending) == MatchStatus.VOIDED

    with pytest.raises(MatchStateError):
        _correct(pending, players[0].account, players[:2], players[2:])


def test_without_as_player_a_participant_is_refused(
    pending: Match, players: list[Player]
) -> None:
    with pytest.raises(MatchActionDenied):
        edit_match(
            pending,
            actor=players[0].account,
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=NEW_GAMES,
            played_on=WORKFLOW_PLAYED_ON,
        )

    assert _status(pending) == MatchStatus.PENDING


def test_a_moderator_with_as_player_is_refused(
    confirmed: Match, moderator: User, players: list[Player]
) -> None:
    with pytest.raises(MatchActionDenied):
        _correct(confirmed, moderator, players[:2], players[2:])

    assert _status(confirmed) == MatchStatus.CONFIRMED


def test_a_player_gives_no_reason(pending: Match, players: list[Player]) -> None:
    with pytest.raises(ReasonRefused):
        _correct(
            pending, players[0].account, players[:2], players[2:], reason="Made up."
        )

    assert _status(pending) == MatchStatus.PENDING


def test_a_non_participant_is_refused(pending: Match, players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        _correct(pending, PlayerFactory.create().account, players[:2], players[2:])

    assert _status(pending) == MatchStatus.PENDING


def _event_match(event: Event, players: list[Player]) -> Match:
    member = OrganizerMemberFactory.create(organizer=event.organizer).account
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match


def test_a_participant_of_an_event_match_is_refused(
    event: Event, players: list[Player]
) -> None:
    match = _event_match(event, players)
    dispute_match(match, actor=players[0].account)

    with pytest.raises(MatchActionDenied):
        _correct(match, players[0].account, players[:2], players[2:])

    assert _status(match) == MatchStatus.DISPUTED


def test_can_correct_as_player(pending: Match, players: list[Player]) -> None:
    assert can_correct_as_player(players[1].account, pending)
    assert not can_correct_as_player(PlayerFactory.create().account, pending)
    assert not can_correct_as_player(players[1].account, Match())


def test_an_event_match_cannot_be_corrected_as_player(
    event: Event, players: list[Player]
) -> None:
    match = _event_match(event, players)

    assert not can_correct_as_player(players[1].account, match)
    assert (
        corrects_as_player(
            players[1].account,
            match,
            team_a_ids=[p.pk for p in players[:2]],
            team_b_ids=[p.pk for p in players[2:]],
        )
        is None
    )


def test_corrects_as_player_needs_the_stored_teams(
    pending: Match, players: list[Player]
) -> None:
    a = [p.pk for p in players[:2]]
    b = [p.pk for p in players[2:]]

    assert (
        corrects_as_player(players[0].account, pending, team_a_ids=a, team_b_ids=b)
        == players[0]
    )
    assert (
        corrects_as_player(players[0].account, pending, team_a_ids=b, team_b_ids=a)
        == players[0]
    )
    assert (
        corrects_as_player(players[0].account, pending, team_a_ids=a[:1], team_b_ids=b)
        is None
    )
    assert (
        corrects_as_player(
            PlayerFactory.create().account, pending, team_a_ids=a, team_b_ids=b
        )
        is None
    )
