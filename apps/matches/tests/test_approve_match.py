"""A moderator approves a match: the result stands (#40). Made-up data only.

``approve_match`` reads the status under the match's locks, resolves a
dispute as confirmed, and resolves the match's open flags as approved,
in one transaction.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.matches import workflow
from apps.matches.models import (
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MODERATOR_GROUP, MatchActionDenied
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import (
    MatchStateError,
    approve_match,
    confirm_match,
    dispute_match,
    void_match,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up approval reason."


def _later(players: list[Player]) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def test_non_moderator_is_refused_first(
    confirmed: Match, players: list[Player]
) -> None:
    for actor in (players[0].account, UserFactory.create(is_staff=True)):
        with pytest.raises(MatchActionDenied):
            approve_match(confirmed, actor=actor, reason="")


@pytest.mark.parametrize("reason", ["", "  \n"])
def test_a_reason_is_required(
    confirmed: Match, players: list[Player], moderator: User, reason: str
) -> None:
    dispute_match(confirmed, actor=players[1].account)

    with pytest.raises(ValueError, match="reason"):
        approve_match(confirmed, actor=moderator, reason=reason)

    assert _status(confirmed) == MatchStatus.DISPUTED


def test_disputed_and_flagged_ends_confirmed_with_flags_approved(
    confirmed: Match, players: list[Player], moderator: User
) -> None:
    later = _later(players)
    dispute_match(later, actor=players[1].account)

    assert approve_match(later, actor=moderator, reason=REASON) is True

    assert _status(later) == MatchStatus.CONFIRMED
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert later.is_counted()
    assert confirmed.is_counted()
    outcome = AuditEvent.objects.get(action=AuditAction.DISPUTE_OUTCOME)
    assert outcome.reason == REASON
    resolved = AuditEvent.objects.get(action=AuditAction.MATCH_FLAG_RESOLVE)
    assert resolved.reason == REASON


def test_a_voided_match_is_refused(confirmed: Match, moderator: User) -> None:
    void_match(confirmed, actor=moderator, reason=REASON)
    before = AuditEvent.objects.count()

    with pytest.raises(MatchStateError):
        approve_match(confirmed, actor=moderator, reason=REASON)

    assert AuditEvent.objects.count() == before


def test_nothing_to_approve_returns_false(confirmed: Match, moderator: User) -> None:
    before = AuditEvent.objects.count()

    assert approve_match(confirmed, actor=moderator, reason=REASON) is False

    assert AuditEvent.objects.count() == before
    assert _status(confirmed) == MatchStatus.CONFIRMED


def _dispute_on_first_lock(
    monkeypatch: pytest.MonkeyPatch, match: Match, player: Player
) -> None:
    """A participant's dispute commits just before the service's locks."""
    real_lock = workflow._lock
    fired: list[bool] = []

    def lock(target: Match) -> Match:
        if not fired:
            fired.append(True)
            dispute_match(match, actor=player.account)
        return real_lock(target)

    monkeypatch.setattr(workflow, "_lock", lock)


def test_status_is_read_under_the_lock(
    confirmed: Match,
    players: list[Player],
    moderator: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = _later(players)
    _dispute_on_first_lock(monkeypatch, later, players[1])

    assert approve_match(later, actor=moderator, reason=REASON) is True

    assert _status(later) == MatchStatus.CONFIRMED
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED


def test_admin_approve_reads_the_status_under_the_lock(
    client: Client,
    confirmed: Match,
    players: list[Player],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    client.force_login(account)
    later = _later(players)
    _dispute_on_first_lock(monkeypatch, later, players[1])

    response = client.post(
        reverse("admin:matches_match_changelist") + "?review=all",
        {
            "action": "approve_selected",
            "_selected_action": [str(later.pk)],
            "reason": REASON,
        },
        follow=True,
    )

    assert _status(later) == MatchStatus.CONFIRMED
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    texts: list[Any] = [str(m) for m in get_messages(response.wsgi_request)]
    assert "1 match(es) approved." in texts
