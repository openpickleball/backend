"""A moderator never releases a match of their own chain through a flag (#40).

Resolving a flag also releases the match at its other end. So
``resolve_flags`` and ``approve_match`` refuse a moderator who played in
the correction chain of any match an open flag links to the target, not
only the target's own chain. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
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
from apps.matches.workflow import approve_match, edit_match, resolve_flags
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
def setup(
    confirmed: Match, players: list[Player], moderator: User
) -> tuple[User, Match, Match]:
    """M played in X; another moderator corrects X to X' without M; Z is
    a near duplicate of X'. Returns (M, X', Z)."""
    newcomer = PlayerFactory.create()
    corrected = edit_match(
        confirmed,
        actor=moderator,
        reason=REASON,
        format=MatchFormat.DOUBLES,
        team_a=[newcomer, players[1]],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
    )
    near = record_match(
        format=MatchFormat.DOUBLES,
        team_a=[newcomer, players[1]],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[1].account,
    ).match
    assert MatchFlag.objects.filter(match=near, duplicate_of=corrected).exists()
    m = _grant_moderation(players[0].account)
    # M moderates Z itself, but not X', which is in M's own chain.
    assert moderates(m, near) is True
    assert moderates(m, corrected) is False
    return m, corrected, near


def _unchanged(corrected: Match, near: Match, events: int, signals: SignalLog) -> None:
    assert MatchFlag.objects.get(match=near).resolved_at is None
    assert Match.objects.get(pk=corrected.pk).status == MatchStatus.CONFIRMED
    assert Match.objects.get(pk=near.pk).status == MatchStatus.PENDING
    assert AuditEvent.objects.count() == events
    assert signals.changed == []
    assert signals.confirmed == []


def test_resolve_flags_refuses_releasing_a_match_of_the_moderators_chain(
    setup: tuple[User, Match, Match], signals: SignalLog
) -> None:
    m, corrected, near = setup
    events = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        resolve_flags(near, actor=m, outcome=FlagResolution.DISMISSED, reason=REASON)

    _unchanged(corrected, near, events, signals)


def test_approve_match_refuses_releasing_a_match_of_the_moderators_chain(
    setup: tuple[User, Match, Match], signals: SignalLog
) -> None:
    m, corrected, near = setup
    events = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        approve_match(near, actor=m, reason=REASON)

    _unchanged(corrected, near, events, signals)


def test_an_outside_moderator_still_resolves(
    setup: tuple[User, Match, Match], moderator: User
) -> None:
    _m, _corrected, near = setup

    assert (
        resolve_flags(
            near, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
        )
        == 1
    )
