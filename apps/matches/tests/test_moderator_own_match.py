"""A moderator never moderates a match they played in (#40, hard rule 8).

``moderates(actor, match)`` is True only for a moderator whose Account has
no Player in any match of the match's correction chain. A participating
moderator is refused with the fixed ``MatchActionDenied`` on every
moderator path, and nothing changes. Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MatchActionDenied, moderates
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.workflow import (
    approve_match,
    dispute_match,
    edit_match,
    resolve_dispute,
    resolve_flags,
    void_match,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up moderator reason."


def _grant_moderation(account: User) -> User:
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


@pytest.fixture
def playing_moderator(players: list[Player]) -> User:
    """Player 0's Account, made a moderator."""
    return _grant_moderation(players[0].account)


def _nothing_changed(
    match: Match, status: MatchStatus, events: int, signals: SignalLog
) -> None:
    assert Match.objects.get(pk=match.pk).status == status
    assert AuditEvent.objects.count() == events
    assert signals.changed == []
    assert signals.confirmed == []
    assert not Match.objects.filter(corrects=match).exists()


def _edit_args(players: list[Player]) -> dict[str, Any]:
    return {
        "format": MatchFormat.DOUBLES,
        "team_a": players[:2],
        "team_b": players[2:],
        "games": ((11, 2), (11, 3)),
        "played_on": WORKFLOW_PLAYED_ON,
    }


def test_moderates_is_true_for_an_outside_moderator(
    confirmed: Match, moderator: User
) -> None:
    assert moderates(moderator, confirmed) is True


def test_moderates_is_false_for_a_participant(
    confirmed: Match, playing_moderator: User
) -> None:
    assert moderates(playing_moderator, confirmed) is False


def test_moderates_refuses_non_moderators_and_unsaved(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    assert moderates(players[1].account, confirmed) is False
    assert moderates(UserFactory.build(), confirmed) is False
    unsaved = Match(format=MatchFormat.DOUBLES, played_on=WORKFLOW_PLAYED_ON)
    assert moderates(moderator, unsaved) is False


def test_resolve_dispute_refuses_a_participating_moderator(
    confirmed: Match,
    players: list[Player],
    playing_moderator: User,
    signals: SignalLog,
) -> None:
    dispute_match(confirmed, actor=players[1].account)
    events = AuditEvent.objects.count()
    signals.changed.clear()

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            confirmed,
            actor=playing_moderator,
            outcome=MatchStatus.CONFIRMED,
            reason=REASON,
        )

    _nothing_changed(confirmed, MatchStatus.DISPUTED, events, signals)


def test_approve_match_refuses_a_participating_moderator(
    confirmed: Match,
    players: list[Player],
    playing_moderator: User,
    signals: SignalLog,
) -> None:
    dispute_match(confirmed, actor=players[1].account)
    events = AuditEvent.objects.count()
    signals.changed.clear()

    with pytest.raises(MatchActionDenied):
        approve_match(confirmed, actor=playing_moderator, reason=REASON)

    _nothing_changed(confirmed, MatchStatus.DISPUTED, events, signals)


def test_resolve_flags_refuses_a_participating_moderator(
    confirmed: Match,
    players: list[Player],
    playing_moderator: User,
    signals: SignalLog,
) -> None:
    later = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[1].account,
    ).match
    events = AuditEvent.objects.count()
    signals.changed.clear()

    with pytest.raises(MatchActionDenied):
        resolve_flags(
            later,
            actor=playing_moderator,
            outcome=FlagResolution.APPROVED,
            reason=REASON,
        )

    assert MatchFlag.objects.get().resolved_at is None
    _nothing_changed(later, MatchStatus.PENDING, events, signals)


@pytest.mark.parametrize("reason", ["", REASON])
def test_void_match_refuses_a_participating_moderator(
    confirmed: Match, playing_moderator: User, signals: SignalLog, reason: str
) -> None:
    events = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        void_match(confirmed, actor=playing_moderator, reason=reason)

    _nothing_changed(confirmed, MatchStatus.CONFIRMED, events, signals)


def test_edit_match_refuses_a_participating_moderator(
    confirmed: Match,
    players: list[Player],
    playing_moderator: User,
    signals: SignalLog,
) -> None:
    events = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        edit_match(
            confirmed, actor=playing_moderator, reason=REASON, **_edit_args(players)
        )

    _nothing_changed(confirmed, MatchStatus.CONFIRMED, events, signals)


def test_edit_match_refuses_a_moderator_adding_their_own_player(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    outsider = _grant_moderation(UserFactory.create())
    own = PlayerFactory.create(account=outsider)
    assert moderates(outsider, confirmed) is True
    events = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        edit_match(
            confirmed,
            actor=outsider,
            reason=REASON,
            **_edit_args([*players[:3], own]),
        )

    _nothing_changed(confirmed, MatchStatus.CONFIRMED, events, signals)


def test_the_correction_chain_counts_both_ways(
    confirmed: Match, players: list[Player], moderator: User, signals: SignalLog
) -> None:
    newcomer = PlayerFactory.create()
    correction = edit_match(
        confirmed,
        actor=moderator,
        reason=REASON,
        format=MatchFormat.DOUBLES,
        team_a=[newcomer, players[1]],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
    )
    original_player = _grant_moderation(players[0].account)
    correction_player = _grant_moderation(newcomer.account)

    # Player 0 played only in the original, the newcomer only in the
    # correction: neither moderates either match.
    assert moderates(original_player, correction) is False
    assert moderates(correction_player, confirmed) is False
    assert moderates(moderator, correction) is True

    events = AuditEvent.objects.count()
    signals.changed.clear()
    signals.confirmed.clear()
    with pytest.raises(MatchActionDenied):
        void_match(correction, actor=original_player, reason=REASON)
    _nothing_changed(correction, MatchStatus.CONFIRMED, events, signals)
