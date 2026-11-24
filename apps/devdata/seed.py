"""Build made-up development records through the factories and services.

Every name and email comes from Faker through the factories under
``apps/*/tests/factories.py``, so nothing here is real player data
(CLAUDE.md, hard rule 1). Organizers are verified through
``verify_organizer()`` and Matches go through ``record_match()``, the same
paths real data takes, so the rows obey every rule the services enforce.

This module imports factory_boy and Faker, which are development
dependencies. Only ``seed_dev_data`` imports it, after its DEBUG check.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from random import Random
from typing import TYPE_CHECKING

from django.contrib.auth import get_user_model
from django.db.models import Max
from django.utils import timezone
from faker import Faker

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Club, Organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    BestOf,
    GameTarget,
    MatchFormat,
    MatchStatus,
    ScoringType,
    Team,
)
from apps.matches.services import record_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

ORGANIZERS = 2
CLUBS = 2
MATCHES_PER_PLAYER = 2
PLAYERS_PER_TEAM = {MatchFormat.SINGLES: 1, MatchFormat.DOUBLES: 2}

# A lineup (both teams, in either order) and the date it was played.
type _LineupKey = tuple[frozenset[frozenset[int]], date]


@dataclass(frozen=True)
class SeedSummary:
    """How many of each record one run added."""

    players: int
    clubs: int
    organizers: int
    events: int
    matches: int


@dataclass(frozen=True)
class _Source:
    """Where a match comes from: an Event, maybe at a Club, and who submits it."""

    submitter: "_User"
    first_day: date
    last_day: date
    event: Event | None = None
    club: Club | None = None


def _reset_account_sequence() -> None:
    """Start the email prefix past every existing Account.

    Each Account takes a primary key above the one before, so starting at
    the largest key plus one never repeats a ``player{n}.`` prefix that an
    earlier run used.
    """
    largest = get_user_model().objects.aggregate(largest=Max("pk"))["largest"]
    # factory_boy carries no type hints.
    UserFactory.reset_sequence((largest or 0) + 1)  # type: ignore[no-untyped-call]


def _event_dates(rng: Random, today: date) -> tuple[date, date]:
    start = today - timedelta(days=rng.randint(30, 365))
    end = min(start + timedelta(days=rng.randint(0, 60)), today)
    return start, end


def _game_winners(rng: Random, best_of: int) -> list[Team]:
    """Each game's winner, ending on the game that decides the match."""
    needed = best_of // 2 + 1
    winner = rng.choice([Team.A, Team.B])
    loser = Team.B if winner == Team.A else Team.A
    earlier = [winner] * (needed - 1) + [loser] * rng.randint(0, needed - 1)
    rng.shuffle(earlier)
    return [*earlier, winner]


def _games(rng: Random, best_of: int) -> list[tuple[int, int]]:
    """Scores to 11, each loser 2 or more behind."""
    games = []
    for winner in _game_winners(rng, best_of):
        loser_score = rng.randint(0, GameTarget.ELEVEN - 2)
        if winner == Team.A:
            games.append((int(GameTarget.ELEVEN), loser_score))
        else:
            games.append((loser_score, int(GameTarget.ELEVEN)))
    return games


def _draw(
    rng: Random,
    players: Sequence[Player],
    match_format: MatchFormat,
    first_day: date,
    last_day: date,
    used: set[_LineupKey],
) -> tuple[list[Player], list[Player], date]:
    """Two teams and a date whose lineup and date no earlier match used.

    A repeat would be an exact or near duplicate, which ``record_match``
    refuses or flags, so draw again until the pair is new.
    """
    size = PLAYERS_PER_TEAM[match_format]
    span = (last_day - first_day).days
    while True:
        picked = rng.sample(list(players), 2 * size)
        team_a, team_b = picked[:size], picked[size:]
        played_on = first_day + timedelta(days=rng.randint(0, span))
        key: _LineupKey = (
            frozenset(
                {
                    frozenset(player.pk for player in team_a),
                    frozenset(player.pk for player in team_b),
                }
            ),
            played_on,
        )
        if key not in used:
            used.add(key)
            return team_a, team_b, played_on


def seed(*, players: int) -> SeedSummary:
    """Add one set of made-up records and return what was added.

    Each Match is recorded in its own ``record_match()`` call with no outer
    transaction, because its advisory locks last until the outermost
    transaction ends.
    """
    rng = Faker().random
    today = timezone.localdate()
    _reset_account_sequence()

    kinds = list(EventKind)
    sources: list[_Source] = []
    hosts: list[tuple[_User, Event]] = []
    for index in range(ORGANIZERS):
        organizer: Organizer = verified_organizer()
        member = OrganizerMemberFactory.create(organizer=organizer)
        start, end = _event_dates(rng, today)
        event: Event = EventFactory.create(
            organizer=organizer,
            kind=kinds[index % len(kinds)],
            start_date=start,
            end_date=end,
        )
        hosts.append((member.account, event))
        sources.append(
            _Source(
                submitter=member.account, event=event, first_day=start, last_day=end
            )
        )
    # A match that names a Club needs an Event, and the Club's grant to the
    # Event's Organizer (ADR 0021). Each Club has an admin, who would make
    # the grant, and grants one of the seeded Organizers.
    for index in range(CLUBS):
        club: Club = ClubFactory.create()
        ClubRoleFactory.create(club=club)
        submitter, host = hosts[index % len(hosts)]
        ClubOrganizerGrantFactory.create(club=club, organizer=host.organizer)
        sources.append(
            _Source(
                submitter=submitter,
                event=host,
                club=club,
                first_day=host.start_date,
                last_day=host.end_date,
            )
        )

    pool: list[Player] = PlayerFactory.create_batch(players)

    used: set[_LineupKey] = set()
    formats = [MatchFormat.SINGLES, MatchFormat.DOUBLES]
    matches = MATCHES_PER_PLAYER * players
    for index in range(matches):
        source = sources[index % len(sources)]
        # Advance the format once per pass over the sources, so each
        # source gets both formats (index % 4 would fix index % 2).
        match_format = formats[(index // len(sources)) % len(formats)]
        team_a, team_b, played_on = _draw(
            rng, pool, match_format, source.first_day, source.last_day, used
        )
        best_of = rng.choice(list(BestOf))
        recorded = record_match(
            format=match_format,
            team_a=team_a,
            team_b=team_b,
            games=_games(rng, best_of),
            played_on=played_on,
            best_of=best_of,
            game_target=GameTarget.ELEVEN,
            scoring_type=ScoringType.SIDE_OUT,
            submitted_by_user=source.submitter,
            event=source.event,
            club=source.club,
        )
        if not recorded.created or recorded.match.status != MatchStatus.CONFIRMED:
            raise RuntimeError("A seeded match was not created confirmed.")

    return SeedSummary(
        players=players,
        clubs=CLUBS,
        organizers=ORGANIZERS,
        events=ORGANIZERS,
        matches=matches,
    )
