"""Builders for the engine adapter tests. Made-up data only.

``saved_match`` inserts a Match with chosen players, gender divisions,
games, status, and confirmations, through the factories. It returns the
Match fetched again, with nothing prefetched, and its players by team.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from apps.events.models import Event
from apps.matches.models import (
    BestOf,
    ConfirmationDecision,
    Game,
    GameTarget,
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    ScoringType,
    Team,
)
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.players.models import GenderDivision, Player
from apps.players.tests.factories import PlayerFactory

PLAYED_ON = date(2026, 3, 1)
THREE_GAMES = ((11, 5), (8, 11), (11, 9))

# Two players on each team, one of each division.
MIXED = {
    Team.A: (GenderDivision.MENS, GenderDivision.WOMENS),
    Team.B: (GenderDivision.WOMENS, GenderDivision.MENS),
}


def _player(division: str) -> Player:
    if division:
        player: Player = PlayerFactory.create(gender_division=division)
        return player
    # A removed division (#190, #116): new Players always have one, so the
    # test clears it the way a removal would.
    player = PlayerFactory.create()
    Player.objects.filter(pk=player.pk).update(gender_division="")
    player.refresh_from_db()
    return player


def saved_match(
    *,
    match_format: str = MatchFormat.DOUBLES,
    divisions: Mapping[Team, Sequence[str]] | None = None,
    games: Sequence[tuple[int, int]] = THREE_GAMES,
    best_of: int = BestOf.THREE,
    game_target: int = GameTarget.ELEVEN,
    scoring_type: str = ScoringType.SIDE_OUT,
    event: Event | None = None,
    status: str = MatchStatus.CONFIRMED,
    confirm: Sequence[tuple[Team, int]] = (),
    dispute: Sequence[tuple[Team, int]] = (),
    played_on: date = PLAYED_ON,
    played_at: datetime | None = None,
    played_at_utc_offset: int | None = None,
    sequence: int = 0,
    **extra: object,
) -> tuple[Match, dict[Team, list[Player]]]:
    """Insert a Match and return it fetched again, with its players by team.

    ``divisions`` gives each team's players' gender divisions ("" for a
    removed one); by default each team is one men's and one women's player
    in doubles, and a men's player in singles. ``confirm`` and ``dispute``
    name players as (team, index) for MatchConfirmation rows.
    """
    if divisions is None:
        size = 2 if match_format == MatchFormat.DOUBLES else 1
        divisions = {team: MIXED[team][:size] for team in Team}
    made: Match = MatchFactory.create(
        format=match_format,
        best_of=best_of,
        game_target=game_target,
        scoring_type=scoring_type,
        event=event,
        played_on=played_on,
        played_at=played_at,
        played_at_utc_offset=played_at_utc_offset,
        sequence=sequence,
        participants=False,
        games=False,
        **extra,
    )
    players = {team: [_player(d) for d in divisions[team]] for team in Team}
    for team, members in players.items():
        for player in members:
            MatchParticipant.objects.create(match=made, player=player, team=team)
    for number, (a, b) in enumerate(games, start=1):
        Game.objects.create(
            match=made, game_number=number, team_a_score=a, team_b_score=b
        )
    for decision, chosen in (
        (ConfirmationDecision.CONFIRM, confirm),
        (ConfirmationDecision.DISPUTE, dispute),
    ):
        for team, index in chosen:
            MatchConfirmation.objects.create(
                match=made, player=players[team][index], decision=decision
            )
    if status != MatchStatus.PENDING:
        raw_update(made, status=status)
    return Match.objects.get(pk=made.pk), players
