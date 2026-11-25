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
- The rating format is the one stored on the Match when it was recorded
  (``Match.rating_format``, ADR 0024), never the players' current gender
  divisions, so a division removed later does not move a past match. The
  engine feeds the category matches into overall doubles itself.
- The match type is the Event's kind. A Match with a Club and no Event is
  ``rec_verified``, and one with neither is self-reported (decision F1 in
  #1, ADR 0024). Submitted input never chooses it. Since ADR 0021 no
  submission can create a Match with a Club and no Event, so that mapping
  covers only matches already in the Ledger and their corrections.
- The game format is the scoring type and game target, win by 2, no cap
  (``validate_match``'s rules). Games are ordered by game number.
- ``played_at`` keeps its instant, in a ``datetime.timezone`` of the stored
  UTC offset.
- ``confirmed_by`` holds the Players who confirmed the Match. For a
  self-reported Match a moderator confirmed, it holds every participant,
  since the moderator's decision stands in for the teams (ADR 0024). That
  is read from the audit log, never guessed: a ``dispute.outcome`` Audit
  Event on the Match whose ``after`` status is confirmed, or, for a
  correction, the ``match.edit`` Audit Event on the Match it corrects
  that names it in ``after.corrected_by`` with a ``before`` status of
  confirmed or disputed (the correction was created confirmed; only a
  moderator can edit a match with no Event and no Club). A self-reported
  Match an Event submitter's duplicate confirmed is not a moderator's: it
  keeps only its confirmers.
- The status is always completed: the backend stores only completed
  matches. The backend's own status is confirmation state, and which
  matches count is ``Match.objects.counted()``, not this conversion.

Every value goes through an explicit table. A value a table does not know
raises ``EngineMappingError``, whose message names the field. A table
takes only a ``str`` (or a ``str`` choice) for a text value and an
``int`` (or an ``int`` choice) for a number, never a bool or a float. The
engine refuses subclasses, so every value passed is a plain ``str``,
``int``, ``date``, ``tuple``, or ``frozenset``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import open_engine
from django.conf import settings
from django.db.models import CharField, Exists, OuterRef, Prefetch, QuerySet
from django.db.models.fields.json import KT
from django.db.models.functions import Cast

from apps.audit.models import AuditAction, AuditEvent
from apps.events.models import EventKind
from apps.matches.models import (
    RATING_FORMATS_BY_FORMAT,
    BestOf,
    ConfirmationDecision,
    GameTarget,
    Match,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    RatingFormat,
    ScoringType,
    Team,
)


class EngineMappingError(ValueError):
    """A backend value has no engine value. The message names the field."""


# The tables. Keys are the backend's stored values, as plain values.

_MATCH_FORMATS: Mapping[str, open_engine.RatingFormat] = {
    str(MatchFormat.SINGLES.value): open_engine.RatingFormat.SINGLES,
    str(MatchFormat.DOUBLES.value): open_engine.RatingFormat.DOUBLES,
}

_RATING_FORMATS: Mapping[str, open_engine.RatingFormat] = {
    str(RatingFormat.SINGLES.value): open_engine.RatingFormat.SINGLES,
    str(RatingFormat.DOUBLES.value): open_engine.RatingFormat.DOUBLES,
    str(RatingFormat.MIXED_DOUBLES.value): open_engine.RatingFormat.MIXED_DOUBLES,
    str(RatingFormat.DOUBLES_MEN.value): open_engine.RatingFormat.DOUBLES_MEN,
    str(RatingFormat.DOUBLES_WOMEN.value): open_engine.RatingFormat.DOUBLES_WOMEN,
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

# The game rules validate_match applies: win by 2, no cap.
_WIN_BY = 2

_CONFIRM = str(ConfirmationDecision.CONFIRM.value)


def _look_up[V](table: Mapping[str, V], value: object, what: str, code: str) -> V:
    """The table's value for a text ``value``, or EngineMappingError naming ``what``."""
    if isinstance(value, str) and value in table:
        return table[value]
    raise EngineMappingError(f"Match {code}: the {what} {value!r} has no engine value.")


def _look_up_number(
    table: Mapping[int, int], value: object, what: str, code: str
) -> int:
    """The table's value for a whole-number ``value``; never a bool or a float."""
    if isinstance(value, int) and not isinstance(value, bool) and value in table:
        return table[value]
    raise EngineMappingError(f"Match {code}: the {what} {value!r} has no engine value.")


def engine_version() -> str:
    """Return the version of the installed engine."""
    return open_engine.__version__


def load_config() -> open_engine.Config:
    """The engine config: the published parameter set the settings name.

    Raises ``open_engine.ConfigError`` for a label the installed engine
    does not publish.
    """
    return open_engine.Config.published(settings.OPEN_ENGINE_PARAMETER_SET)


def _rating_format(match: Match, code: str) -> open_engine.RatingFormat:
    """The stored rating format, checked against the format."""
    _look_up(_MATCH_FORMATS, match.format, "format", code)
    rating_format = _look_up(
        _RATING_FORMATS, match.rating_format, "rating format", code
    )
    if match.rating_format not in RATING_FORMATS_BY_FORMAT[match.format]:
        raise EngineMappingError(
            f"Match {code}: the rating format does not fit the format."
        )
    return rating_format


_MODERATOR_RULED = "_engine_moderator_ruled"
_MODERATOR_CORRECTION = "_engine_moderator_correction"


def _with_moderator_actions(matches: QuerySet[Match]) -> QuerySet[Match]:
    """``matches`` annotated with whether a moderator confirmed each one.

    Two subqueries on the audit log, so a list of matches still takes one
    query. ``_engine_moderator_ruled``: a ``dispute.outcome`` Audit Event
    on the match whose ``after`` status is confirmed.
    ``_engine_moderator_correction``: the ``match.edit`` Audit Event on the
    match this one corrects, naming this one in ``after.corrected_by``,
    whose ``before`` status was confirmed or disputed, so this correction
    was created confirmed.
    """
    on_matches = AuditEvent.objects.filter(
        target_content_type__app_label=Match._meta.app_label,
        target_content_type__model=Match._meta.model_name,
    )
    ruled = on_matches.annotate(outcome=KT("after__status")).filter(
        action=AuditAction.DISPUTE_OUTCOME,
        target_object_id=Cast(OuterRef("pk"), output_field=CharField()),
        outcome=MatchStatus.CONFIRMED,
    )
    corrected = on_matches.annotate(
        old_status=KT("before__status"), corrected_by=KT("after__corrected_by")
    ).filter(
        action=AuditAction.MATCH_EDIT,
        target_object_id=Cast(OuterRef("corrects_id"), output_field=CharField()),
        corrected_by=OuterRef("public_code"),
        old_status__in=[MatchStatus.CONFIRMED, MatchStatus.DISPUTED],
    )
    return matches.annotate(
        **{_MODERATOR_RULED: Exists(ruled), _MODERATOR_CORRECTION: Exists(corrected)}
    )


def _moderator_confirmed(match: Match) -> bool:
    """True when a moderator's action confirmed ``match`` (see the module docstring)."""
    if hasattr(match, _MODERATOR_RULED) and hasattr(match, _MODERATOR_CORRECTION):
        flags = (
            getattr(match, _MODERATOR_RULED),
            getattr(match, _MODERATOR_CORRECTION),
        )
    else:
        flags = (
            _with_moderator_actions(Match.objects.filter(pk=match.pk))
            .values_list(_MODERATOR_RULED, _MODERATOR_CORRECTION)
            .get()
        )
    return any(flag is True for flag in flags)


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
    ``EngineMappingError`` for a value with no engine value, and for a
    Match that is not saved.
    """
    if match._state.adding or match.pk is None:
        raise EngineMappingError("Only a saved Match can be converted.")
    code = str(match.public_code)
    rating_format = _rating_format(match, code)

    teams: dict[Team, list[str]] = {team: [] for team in Team}
    codes_by_player: dict[int, str] = {}
    for participant in match.participants.all():
        team = _look_up(_TEAMS, participant.team, "team", code)
        player_code = str(participant.player.public_code)
        teams[team].append(player_code)
        codes_by_player[participant.player_id] = player_code

    # Only the Event, then the Club, decide the match type, never
    # submitted input (ADR 0024). A Club with no Event is only a match
    # already in the Ledger, or its correction: none can be submitted since
    # ADR 0021.
    event = match.event
    if event is not None:
        match_type = _look_up(_MATCH_TYPES, event.kind, "event kind", code)
    elif match.club_id is not None:
        match_type = open_engine.MatchType.REC_VERIFIED
    else:
        match_type = open_engine.MatchType.SELF_REPORTED

    game_format = open_engine.GameFormat(
        scoring=_look_up(_SCORING_TYPES, match.scoring_type, "scoring type", code),
        points_to=_look_up_number(
            _GAME_TARGETS, match.game_target, "game target", code
        ),
        win_by=_WIN_BY,
        cap=None,
    )
    games = tuple(
        (int(game.team_a_score), int(game.team_b_score))
        for game in sorted(match.games.all(), key=lambda game: game.game_number)
    )

    confirmed_by: set[str] = set()
    if match_type is open_engine.MatchType.SELF_REPORTED and _moderator_confirmed(
        match
    ):
        # The moderator's decision stands in for both teams (ADR 0024).
        confirmed_by.update(codes_by_player.values())
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
        rating_format=rating_format,
        match_type=match_type,
        best_of=_look_up_number(_BEST_OF, match.best_of, "best of", code),
        game_format=game_format,
        played_at=_played_at(match, code),
        sequence=int(match.sequence),
        confirmed_by=frozenset(confirmed_by),
        status=open_engine.MatchStatus.COMPLETED,
    )


def counted_engine_matches() -> list[open_engine.Match]:
    """Every match that counts (``Match.objects.counted()``), converted.

    Four queries whatever the number of matches: the matches with their
    Events and the audit log checks for moderator actions, the
    participants with their Players, the games, and the confirmations. Ordered by date played, then by row, for a stable list;
    the engine orders its ledger itself.
    """
    matches = (
        _with_moderator_actions(Match.objects.counted())
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


@dataclass(frozen=True)
class RatingSummary:
    """A player's rating in one format, as plain data (#45).

    The fields are ``open_engine.PlayerSummary``'s that the backend stores,
    with the rating format as its ``str`` value and every date a plain
    ``date``. The values are the engine's own, never rounded or computed
    here (hard rule 2). ``reliability`` is OPEN-earned and
    ``imported_reliability`` the carried external reliability, kept apart
    (hard rule 5). The seed provenance fields (``initial_*``, #122),
    ``account_linked``, and ``connectivity`` are not stored, so they are
    left out.
    """

    player_id: str
    rating_format: str
    rating: float
    rd: float
    reliability: float
    imported_reliability: float | None
    established: bool
    matches_played: int
    last_played: date | None
    calculated_as_of: date | None
    live_updates_since_recalculation: int
    config_version: str
    params_version: str | None
    parameter_fingerprint: str


def _plain_date(value: date) -> date:
    return date(value.year, value.month, value.day)


def _plain_optional_date(value: date | None) -> date | None:
    return None if value is None else _plain_date(value)


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


def rating_summary(summary: open_engine.PlayerSummary) -> RatingSummary:
    """Convert an engine ``PlayerSummary`` to a ``RatingSummary``."""
    return RatingSummary(
        player_id=str(summary.player_id),
        rating_format=str(summary.rating_format.value),
        rating=float(summary.rating),
        rd=float(summary.rd),
        reliability=float(summary.reliability),
        imported_reliability=(
            None
            if summary.imported_reliability is None
            else float(summary.imported_reliability)
        ),
        established=bool(summary.established),
        matches_played=int(summary.matches_played),
        last_played=_plain_optional_date(summary.last_played),
        calculated_as_of=_plain_optional_date(summary.calculated_as_of),
        live_updates_since_recalculation=int(summary.live_updates_since_recalculation),
        config_version=str(summary.config_version),
        params_version=(
            None if summary.params_version is None else str(summary.params_version)
        ),
        parameter_fingerprint=str(summary.parameter_fingerprint),
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


# Live updates (#46, ADR 0027).


@dataclass(frozen=True)
class LiveConfig:
    """The loaded config's labels, as plain data.

    ``parameter_fingerprint`` keys the stored engine snapshot.
    """

    parameter_fingerprint: str
    config_fingerprint: str
    config_version: str
    params_version: str | None


@dataclass(frozen=True)
class LiveUpdate:
    """What recording one Match live did, as plain data.

    ``summaries`` holds each player's summary and the engine's delta, for
    every format whose update counted: the Match's own format, plus overall
    doubles for a category doubles match whose parent update counted.
    Ordered by format, then player code. ``late`` is True when the engine
    found the match (or its overall doubles update) earlier than a
    player's last match. ``snapshot`` is the engine's ``to_snapshot()``
    after recording; save it only when the update counted and was not
    late. ``already_recorded`` is True when the snapshot had the Match
    already (a redelivery): nothing was recorded.
    """

    counted: bool
    reason: str | None
    late: bool
    already_recorded: bool
    summaries: tuple[tuple[RatingSummary, float], ...]
    snapshot: dict[str, object]
    parameter_fingerprint: str
    config_fingerprint: str
    config_version: str
    params_version: str | None


def _live_config(cfg: open_engine.Config) -> LiveConfig:
    return LiveConfig(
        parameter_fingerprint=str(cfg.parameter_fingerprint()),
        config_fingerprint=str(cfg.fingerprint()),
        config_version=str(cfg.version),
        params_version=None if cfg.params_version is None else str(cfg.params_version),
    )


def live_config() -> LiveConfig:
    """The labels of the config ``load_config()`` loads."""
    return _live_config(load_config())


def _summaries(
    engine: open_engine.RatingEngine, result: open_engine.MatchResult
) -> tuple[tuple[RatingSummary, float], ...]:
    """Each counted format's summaries, with the engine's delta for each player."""
    updates = [result]
    if result.parent is not None and result.parent.counted:
        updates.append(result.parent)
    found: list[tuple[RatingSummary, float]] = []
    for update in updates:
        rating_format = update.rating_format
        for player_id, delta in update.deltas.items():
            last_played = engine.state(player_id, rating_format).last_played
            # The engine's own default reference (the later of the last
            # replay and the player's last match), passed explicitly since
            # player_summary refuses no as_of before any replay.
            dates = [d for d in (engine.calculated_as_of, last_played) if d is not None]
            summary = engine.player_summary(player_id, rating_format, as_of=max(dates))
            found.append((rating_summary(summary), float(delta)))
    found.sort(key=lambda item: (item[0].rating_format, item[0].player_id))
    return tuple(found)


def snapshot_holds_match(snapshot: Mapping[str, object] | None, match: Match) -> bool:
    """True when the engine ``snapshot`` holds has already processed ``match``.

    Looks for the Match's OPEN Code in the engine's processed match ids.
    False for an empty snapshot, and False, not an error, when the
    snapshot cannot be loaded under the loaded config (another config or
    snapshot schema): such a snapshot holds nothing a live update can use.
    """
    if not snapshot:
        return False
    try:
        engine = open_engine.RatingEngine.from_snapshot(snapshot, load_config())
    except (open_engine.SnapshotError, open_engine.ConfigError):
        return False
    return str(match.public_code) in engine.processed_match_ids


def record_live(snapshot: Mapping[str, object] | None, match: Match) -> LiveUpdate:
    """Record ``match`` live on the engine ``snapshot`` holds, and return the result.

    ``snapshot`` is an earlier ``to_snapshot()`` output under the loaded
    config, or None for an empty engine. Pass only a Match that counts
    (``Match.objects.counted()``). The match is recorded without
    ``as_of``, so the engine labels it a live update. Raises
    ``EngineMappingError`` for a Match with no engine value, and the
    engine's own errors for a snapshot it refuses (``SnapshotError``,
    ``ConfigError``).
    """
    cfg = load_config()
    labels = _live_config(cfg)
    engine = (
        open_engine.RatingEngine(cfg)
        if snapshot is None
        else open_engine.RatingEngine.from_snapshot(snapshot, cfg)
    )
    engine_match = to_engine_match(match)
    if engine_match.match_id in engine.processed_match_ids:
        return LiveUpdate(
            counted=True,
            reason=None,
            late=False,
            already_recorded=True,
            summaries=(),
            snapshot=dict(snapshot) if snapshot is not None else engine.to_snapshot(),
            **vars(labels),
        )
    result = engine.record(engine_match)
    counted = bool(result.counted)
    late = bool(result.late) or (result.parent is not None and bool(result.parent.late))
    return LiveUpdate(
        counted=counted,
        reason=None if result.reason is None else str(result.reason.value),
        late=late,
        already_recorded=False,
        summaries=_summaries(engine, result) if counted else (),
        snapshot=engine.to_snapshot(),
        **vars(labels),
    )


# Full replays (#47).


@dataclass(frozen=True)
class ReplayRun:
    """What one full replay of the ledger produced, as plain data.

    ``provenance`` is the replay's (hard rule 4). ``summaries`` holds a
    summary for every player and format the replayed engine holds a state
    in, ordered by format, then player code. ``snapshot`` is the engine's
    ``to_snapshot()`` taken right after the replay, so it can be resumed
    from. The labels are the loaded config's.
    """

    provenance: ReplayProvenance
    summaries: tuple[RatingSummary, ...]
    snapshot: dict[str, object]
    parameter_fingerprint: str
    config_fingerprint: str
    config_version: str
    params_version: str | None


def run_replay(matches: list[open_engine.Match], as_of: date) -> ReplayRun:
    """Replay ``matches`` through the engine as of ``as_of`` and return the result.

    Pass every match that counts (``counted_engine_matches()``): the
    engine rebuilds every rating from them, and skips a row dated after
    ``as_of``. The snapshot is taken first, before any summary is read,
    so it is exactly what the replay produced. The players and formats
    come from the engine's public ``players`` and each player's
    ``ratings``. Each summary takes the engine's default reference date
    (no ``as_of``), which after a replay is the replay date or the
    player's last match, whichever is later. Raises the engine's own
    errors.
    """
    cfg = load_config()
    labels = _live_config(cfg)
    engine = open_engine.replay(matches, cfg=cfg, as_of=_plain_date(as_of))
    snapshot = engine.to_snapshot()
    info = engine.replay_info
    if info is None:  # pragma: no cover (replay always sets it)
        raise RuntimeError("The engine returned no replay provenance.")
    summaries = sorted(
        (
            rating_summary(engine.player_summary(player_id, rating_format))
            for player_id, player in engine.players.items()
            for rating_format in player.ratings
        ),
        key=lambda summary: (summary.rating_format, summary.player_id),
    )
    return ReplayRun(
        provenance=replay_provenance(info),
        summaries=tuple(summaries),
        snapshot=snapshot,
        **vars(labels),
    )
