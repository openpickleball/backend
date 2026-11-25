"""A player's dispute notice promises only actions someone can take (#272, F2).

The Organizer settles a dispute only when the match's Event has a
verified Organizer, and the players correct the scores only on a
self-reported match (no Event, no Club). Both are read from the stored
match. Made-up data only.
"""

import pytest
from django.core.mail import EmailMultiAlternatives

from apps.clubs.tests.factories import ClubFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.emails import render_dispute_notice
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.tests.factories import MatchFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

WITHDRAW = "The player who disputed it can withdraw the dispute."
ORGANIZER = "The event's organizer can settle it."
CORRECT = (
    "Either team can correct the scores from the match's page; the correction "
    "counts once the other team confirms it."
)


def _disputed(**links: object) -> Match:
    match: Match = MatchFactory.create(
        format=MatchFormat.SINGLES, status=MatchStatus.DISPUTED, **links
    )
    return match


def _texts(match: Match) -> list[str]:
    account = PlayerFactory.create().account
    message: EmailMultiAlternatives = render_dispute_notice(
        account, match, team=Team.A, ends_on=match.played_on
    )
    [(html, _mimetype)] = message.alternatives
    return [str(message.body), str(html)]


def test_unverified_organizer_event_match_promises_neither() -> None:
    for text in _texts(_disputed(event=EventFactory.create())):
        assert WITHDRAW in text
        assert ORGANIZER not in text
        assert CORRECT not in text


def test_club_only_match_promises_neither() -> None:
    for text in _texts(_disputed(club=ClubFactory.create())):
        assert WITHDRAW in text
        assert ORGANIZER not in text
        assert CORRECT not in text


def test_self_reported_match_offers_the_correction() -> None:
    for text in _texts(_disputed()):
        assert WITHDRAW in text
        assert CORRECT in text
        assert ORGANIZER not in text


def test_verified_organizer_event_match_offers_the_organizer() -> None:
    event = EventFactory.create(organizer=verified_organizer())
    for text in _texts(_disputed(event=event)):
        assert WITHDRAW in text
        assert ORGANIZER in text
        assert CORRECT not in text
