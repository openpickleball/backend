"""A moderator never voids a match they played in (#40, hard rule 8).

``moderates(actor, match)`` is the bulk void's per-match rule (#273): it
is True only for a moderator whose Account has no Player in any match of
the match's correction chain, in either direction. The bulk void counts
a match it refuses as ``refused`` (``test_bulk_void.py``). Made-up data
only.
"""

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchFormat
from apps.matches.permissions import moderates
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_GAMES, WORKFLOW_PLAYED_ON
from apps.matches.workflow import edit_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


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


def test_the_correction_chain_counts_both_ways(
    event: Event, players: list[Player], moderator: User
) -> None:
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    original = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=WORKFLOW_GAMES,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match
    newcomer = PlayerFactory.create()
    correction = edit_match(
        original,
        actor=member,
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
    assert moderates(correction_player, original) is False
    assert moderates(moderator, correction) is True
