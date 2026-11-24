"""The backend's one link to the rating engine (``open_engine``).

This is the only application module that imports ``open_engine``, tests
aside (a test checks it). The backend never computes a rating itself (hard
rule 2): it converts its records to the engine's types, calls the engine,
and converts the engine's results to plain data. The engine is pinned to a
git tag in ``pyproject.toml`` and ``uv.lock`` (see ``docs/engine.md``).

Converting a Match (``to_engine_match``):

- ``match_id`` is the Match's OPEN Code, and each player id is the
  Player's OPEN Code. A correction is a new Match with a new code.
- Team A and team B come from each participant's team, each sorted by
  code, so the output is deterministic.
- The rating format is singles for a singles match. For doubles it comes
  from the players' gender divisions: four men's players are men's
  doubles, four women's players are women's doubles, one of each on each
  team is mixed doubles, and any other mix, or a player whose division was
  removed, is overall doubles. The engine feeds the category matches into
  overall doubles itself.
- The match type is the Event's kind. A Match with no Event is
  self-reported (decision F1 in #1), whatever a submitter sent and whether
  or not it has a Club.
- The game format is the scoring type and game target, win by 2, no cap
  (``validate_match``'s rules). Games are ordered by game number.
- ``played_at`` keeps its instant, in a ``datetime.timezone`` of the stored
  UTC offset.
- ``confirmed_by`` holds the Players who confirmed the Match.
- The status is always completed: the backend stores only completed
  matches. The backend's own status is confirmation state, and which
  matches count is ``Match.objects.counted()``, not this conversion.

Every value goes through an explicit table. A value a table does not know
raises ``EngineMappingError``, whose message names the field and never a
gender division value (personal data, ADR 0009). The engine refuses
subclasses, so every value passed is a plain ``str``, ``int``, ``date``,
``tuple``, or ``frozenset``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import open_engine
from django.conf import settings
from django.db.models import Prefetch

from apps.events.models import EventKind
from apps.matches.models import (
    BestOf,
    ConfirmationDecision,
    GameTarget,
    Match,
    MatchFormat,
    MatchParticipant,
    ScoringType,
    Team,
)
from apps.players.models import GenderDivision


class EngineMappingError(ValueError):
    """A backend value has no engine value. The message names the field."""


# The tables. Keys are the backend's stored values, as plain values.

_MATCH_FORMATS: Mapping[str, open_engine.RatingFormat] = {
    str(MatchFormat.SINGLES.value): open_engine.RatingFormat.SINGLES,
    str(MatchFormat.DOUBLES.value): open_engine.RatingFormat.DOUBLES,
}

_MATCH_TYPES: Mapping[str, open_engine.MatchType] = {
    str(EventKind.TOURNAMENT.value): open_engine.MatchType.TOURNAMENT,
    str(EventKind.LEAGUE.value): open_engine.MatchType.LEAGUE,
    str(EventKind.REC_VERIFIED.value): open_engine.MatchType.REC_VERIFIED,
}

_SCORING_TYPES: Mapping[str, open_engine.ScoringType] = {
    str(ScoringType.SIDE_OUT.value): open_engine.ScoringType.SIDE_OUT,
    str(ScoringType.RALLY.value): open_engine.ScoringType.RALLY,
}

_GAME_TARGETS: Mapping[int, int] = {
    int(GameTarget.ELEVEN.value): 11,
    int(GameTarget.FIFTEEN.value): 15,
    int(GameTarget.TWENTY_ONE.value): 21,
}

_BEST_OF: Mapping[int, int] = {
    int(BestOf.ONE.value): 1,
    int(BestOf.THREE.value): 3,
    int(BestOf.FIVE.value): 5,
}

_TEAMS: Mapping[str, Team] = {str(team.value): team for team in Team}

# A removed division (#190, #116) is empty, and counts as no division.
_MENS = str(GenderDivision.MENS.value)
_WOMENS = str(GenderDivision.WOMENS.value)
_DIVISIONS: frozenset[str] = frozenset({_MENS, _WOMENS, ""})

# The game rules validate_match applies: win by 2, no cap.
_WIN_BY = 2

_CONFIRM = str(ConfirmationDecision.CONFIRM.value)


def _look_up[K, V](table: Mapping[K, V], value: object, what: str, code: str) -> V:
    """The table's value for ``value``, or EngineMappingError naming ``what``."""
    try:
        return table[value]  # type: ignore[index]
    except (KeyError, TypeError):
        raise EngineMappingError(
            f"Match {code}: the {what} {value!r} has no engine value."
        ) from None


def engine_version() -> str:
    """Return the version of the installed engine."""
    return open_engine.__version__


def load_config() -> open_engine.Config:
    """The engine config: the published parameter set the settings name.

    Raises ``open_engine.ConfigError`` for a label the installed engine
    does not publish.
    """
    return open_engine.Config.published(settings.OPEN_ENGINE_PARAMETER_SET)


def _rating_format(
    code: str, base: open_engine.RatingFormat, divisions: Mapping[Team, list[str]]
) -> open_engine.RatingFormat:
    for members in divisions.values():
        for division in members:
            if division not in _DIVISIONS:
                # Never the value: a gender division is personal data.
                raise EngineMappingError(
                    f"Match {code}: a player's gender division has no engine value."
                )
    if base is not open_engine.RatingFormat.DOUBLES:
        return base
    every = [d for members in divisions.values() for d in members]
    if every and all(d == _MENS for d in every):
        return open_engine.RatingFormat.DOUBLES_MEN
    if every and all(d == _WOMENS for d in every):
        return open_engine.RatingFormat.DOUBLES_WOMEN
    if all(sorted(members) == [_MENS, _WOMENS] for members in divisions.values()):
        return open_engine.RatingFormat.MIXED_DOUBLES
    return open_engine.RatingFormat.DOUBLES


def _played_at(match: Match, code: str) -> datetime | None:
    played_at, offset = match.played_at, match.played_at_utc_offset
    if played_at is None and offset is None:
        return None
    if played_at is None or offset is None:
        raise EngineMappingError(
            f"Match {code}: played_at and its UTC offset must both be set or both empty."
        )
    if played_at.tzinfo is None or played_at.utcoffset() is None:
        raise EngineMappingError(f"Match {code}: played_at has no time zone.")
    if type(offset) is not int:
        raise EngineMappingError(
            f"Match {code}: the played_at offset is not a whole number."
        )
    zone = timezone(timedelta(minutes=offset))
    local = played_at.astimezone(zone)
    # The engine takes exactly a datetime, never a subclass.
    return datetime(
        local.year,
        local.month,
        local.day,
        local.hour,
        local.minute,
        local.second,
        local.microsecond,
        tzinfo=zone,
    )


def to_engine_match(match: Match) -> open_engine.Match:
    """Convert a saved backend Match to an ``open_engine.Match``.

    Reads the Match's participants and their Players, games,
    confirmations, and Event, through any prefetched rows (see
    ``counted_engine_matches``). It does not choose which matches count:
    pass only ``Match.objects.counted()`` rows. Raises
    ``EngineMappingError`` for a value with no engine value.
    """
    code = str(match.public_code)
    base = _look_up(_MATCH_FORMATS, match.format, "format", code)

    teams: dict[Team, list[str]] = {team: [] for team in Team}
    divisions: dict[Team, list[str]] = {team: [] for team in Team}
    codes_by_player: dict[int, str] = {}
    for participant in match.participants.all():
        team = _look_up(_TEAMS, participant.team, "team", code)
        player = participant.player
        player_code = str(player.public_code)
        teams[team].append(player_code)
        divisions[team].append(str(player.gender_division))
        codes_by_player[participant.player_id] = player_code

    # Only the Event decides the match type, never submitted input.
    event = match.event
    if event is None:
        match_type = open_engine.MatchType.SELF_REPORTED
    else:
        match_type = _look_up(_MATCH_TYPES, event.kind, "event kind", code)

    game_format = open_engine.GameFormat(
        scoring=_look_up(_SCORING_TYPES, match.scoring_type, "scoring type", code),
        points_to=_look_up(_GAME_TARGETS, match.game_target, "game target", code),
        win_by=_WIN_BY,
        cap=None,
    )
    games = tuple(
        (int(game.team_a_score), int(game.team_b_score))
        for game in sorted(match.games.all(), key=lambda game: game.game_number)
    )

    confirmed_by: set[str] = set()
    for confirmation in match.confirmations.all():
        if confirmation.decision != _CONFIRM:
            continue
        try:
            confirmed_by.add(codes_by_player[confirmation.player_id])
        except KeyError:
            raise EngineMappingError(
                f"Match {code}: confirmed by a player who did not play in it."
            ) from None

    played_on = match.played_on
    return open_engine.Match(
        match_id=code,
        team_a=tuple(sorted(teams[Team.A])),
        team_b=tuple(sorted(teams[Team.B])),
        games=games,
        played_on=date(played_on.year, played_on.month, played_on.day),
        rating_format=_rating_format(code, base, divisions),
        match_type=match_type,
        best_of=_look_up(_BEST_OF, match.best_of, "best of", code),
        game_format=game_format,
        played_at=_played_at(match, code),
        sequence=int(match.sequence),
        confirmed_by=frozenset(confirmed_by),
        status=open_engine.MatchStatus.COMPLETED,
    )


def counted_engine_matches() -> list[open_engine.Match]:
    """Every match that counts (``Match.objects.counted()``), converted.

    Four queries whatever the number of matches: the matches with their
    Events, the participants with their Players, the games, and the
    confirmations. Ordered by date played, then by row, for a stable list;
    the engine orders its ledger itself.
    """
    matches = (
        Match.objects.counted()
        .select_related("event")
        .prefetch_related(
            Prefetch(
                "participants",
                queryset=MatchParticipant.objects.select_related("player"),
            ),
            "games",
            "confirmations",
        )
        .order_by("played_on", "pk")
    )
    return [to_engine_match(match) for match in matches]


# Results as plain data.


@dataclass(frozen=True)
class RatingUpdate:
    """What the engine did with one match in one rating format, as plain data.

    The fields are ``open_engine.MatchResult``'s, with each enum as its
    ``str`` value. ``parent`` is the overall doubles update for a category
    match. ``possible_duplicate_of`` names another report: never show it to
    the submitter.
    """

    counted: bool
    rating_format: str
    reason: str | None
    expected_a: float
    actual_a: float
    surprise: float
    effective_weight: float
    deltas: dict[str, float]
    frozen: tuple[str, ...]
    clamped: tuple[str, ...]
    imbalance: float
    late: bool
    parent: "RatingUpdate | None"
    possible_duplicate_of: str | None


@dataclass(frozen=True)
class ReplayProvenance:
    """One replay's provenance, as plain data (hard rule 4).

    The fields are ``open_engine.ReplayInfo``'s, with
    ``net_rating_created`` keyed by each rating format's ``str`` value,
    plus the installed engine's version.
    """

    as_of: date
    config_version: str
    config_fingerprint: str
    params_version: str | None
    parameter_fingerprint: str
    matches_considered: int
    matches_counted: int
    net_rating_created: dict[str, float]
    engine_version: str


def _plain_date(value: date) -> date:
    return date(value.year, value.month, value.day)


def rating_update(result: open_engine.MatchResult) -> RatingUpdate:
    """Convert an engine ``MatchResult`` to a ``RatingUpdate``."""
    return RatingUpdate(
        counted=bool(result.counted),
        rating_format=str(result.rating_format.value),
        reason=None if result.reason is None else str(result.reason.value),
        expected_a=float(result.expected_a),
        actual_a=float(result.actual_a),
        surprise=float(result.surprise),
        effective_weight=float(result.effective_weight),
        deltas={str(player): float(delta) for player, delta in result.deltas.items()},
        frozen=tuple(str(player) for player in result.frozen),
        clamped=tuple(str(player) for player in result.clamped),
        imbalance=float(result.imbalance),
        late=bool(result.late),
        parent=None if result.parent is None else rating_update(result.parent),
        possible_duplicate_of=(
            None
            if result.possible_duplicate_of is None
            else str(result.possible_duplicate_of)
        ),
    )


def replay_provenance(info: open_engine.ReplayInfo) -> ReplayProvenance:
    """Convert an engine ``ReplayInfo`` to a ``ReplayProvenance``."""
    return ReplayProvenance(
        as_of=_plain_date(info.as_of),
        config_version=str(info.config_version),
        config_fingerprint=str(info.config_fingerprint),
        params_version=None
        if info.params_version is None
        else str(info.params_version),
        parameter_fingerprint=str(info.parameter_fingerprint),
        matches_considered=int(info.matches_considered),
        matches_counted=int(info.matches_counted),
        net_rating_created={
            str(rating_format.value): float(value)
            for rating_format, value in info.net_rating_created.items()
        },
        engine_version=engine_version(),
    )
