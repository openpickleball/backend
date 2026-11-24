"""Score validation and recording: how a match submission enters the ledger.

Every path that submits a Match (the results API, CSV upload, organizer
tools, partner imports) calls ``record_match``, which calls
``validate_match`` before it writes anything, so an impossible score never
reaches the ledger (CLAUDE.md, hard rule 3). ``validate_match`` takes plain
values and does no database access.

Before anything else, ``record_match`` checks that the submitter may
submit the match (``apps.matches.permissions``) and raises
``MatchPermissionDenied`` if not. A refused submitter gets no validation
detail and no duplicate error, so it cannot learn whether a match exists.
Next it checks that a Submission Batch it was given fits the submission
(``BATCH_REFUSED``), so a bulk void of that batch reaches only matches its
submitter sent.

After that check, ``record_match`` runs every model rule on the Match, its
participants, and its games, so nothing invalid reaches a lock, a lookup,
or the database. It then detects duplicates (a security-sensitive area,
CLAUDE.md hard rule 8). In one transaction, under advisory locks, it:

- refuses an external id the same Partner Application already used, for a
  Match in any status (``DuplicateKind.DUPLICATE_EXTERNAL_ID``);
- refuses an exact duplicate: a Match that is not voided, with the same
  fingerprint, that is the same two teams in either order, the same date
  played, and the same game scores (``DuplicateKind.DUPLICATE_MATCH``);
- saves a near duplicate (a Match that is not voided, with the same two
  teams and date but other scores) and flags it with one ``MatchFlag`` per
  earlier Match.

A refusal raises ``DuplicateMatchError``. It names the earlier Match's
OPEN code only when the submitter may know it: the same Account or
Partner Application submitted that Match, or the submitting Account's
Player played in it. Anyone else learns only that such a match exists.
The database backs the exact rule with a partial unique constraint
on the fingerprint, and the external id rule with its own constraint. A
concurrent insert that trips either still raises ``DuplicateMatchError``.
The same teams playing the same scores twice on one day count as one
match: ``sequence`` is not part of the fingerprint.

The rest of this docstring describes ``validate_match``.

Players are any hashable reference, since each path resolves players its
own way, but each player must be given as exactly one canonical reference:
a resolved Player, or an OPEN Code already passed through
``apps.public_ids.codes.normalize()``. Never pass raw input. Every
reference in one call must be the same kind (the same type): a call that
mixes kinds is refused with ``mixed_player_references``. The duplicate and
both-teams checks compare references by equality, so two spellings of one
code, or a Player and its code, would count as two players. Teams and the game list are lists or
tuples; anything else (a string, a generator, a dict) is an issue.

Defaults: a missing game target means 11 and a missing scoring type means
side-out. A missing best-of is inferred by ``infer_best_of``: the smallest
allowed best-of whose winning number of games the last game reaches, with
no game after it. Scoring type changes no rule: a game is won at the
target by 2 or more, or past the target by exactly 2.

A failure raises one ``MatchValidationError`` with every issue found. Each
issue has a field, a code, and a message, the shape of the API's
``FieldError``. Messages are fixed text, game numbers, and allowed values,
never a submitted value (CLAUDE.md, API).

No rule reads payment status or any paid entitlement: the function takes
no such input (CLAUDE.md, hard rule 7).
"""

import hashlib
import json
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypeGuard

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from oauth2_provider.models import get_application_model

from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event, snapshot
from apps.matches.models import (
    MAX_GAMES,
    MAX_UTC_OFFSET_MINUTES,
    BestOf,
    FlagReason,
    Game,
    GameTarget,
    Match,
    MatchFlag,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    RatingFormat,
    ScoringType,
    SubmissionBatch,
    Team,
)
from apps.matches.permissions import Actor, MatchSubmission, check_can_create_match
from apps.matches.signals import MatchChange, match_changed, match_confirmed
from apps.players.models import GenderDivision, Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

    from apps.clubs.models import Club
    from apps.events.models import Event
    from apps.partners.models import PartnerApplication

PLAYERS_PER_TEAM: dict[MatchFormat, int] = {
    MatchFormat.SINGLES: 1,
    MatchFormat.DOUBLES: 2,
}
WIN_MARGIN = 2

_MENS = str(GenderDivision.MENS.value)
_WOMENS = str(GenderDivision.WOMENS.value)
# An empty division is one a removal cleared (#190, #116).
_KNOWN_DIVISIONS = frozenset({_MENS, _WOMENS, ""})
_UNKNOWN_DIVISION = "A player's gender division is not a known division."
_UNKNOWN_FORMAT = "The match format is not singles or doubles."

BATCH_REFUSED = "This submission batch does not fit this match."


def derive_rating_format(
    format: str, team_a: Sequence[str], team_b: Sequence[str]
) -> RatingFormat:
    """The rating format of a match, from its format and its players' divisions.

    ``team_a`` and ``team_b`` hold each player's gender division. Singles is
    ``singles``. In doubles, four men's players are ``doubles_men``, four
    women's players ``doubles_women``, one of each on each team
    ``mixed_doubles``, and any other mix, or a player with no division,
    ``doubles`` (ADR 0024). Raises ``ValueError`` for an unknown format or
    division; the message never names a division (ADR 0009).
    """
    divisions = [*team_a, *team_b]
    if any(division not in _KNOWN_DIVISIONS for division in divisions):
        raise ValueError(_UNKNOWN_DIVISION)
    if format == MatchFormat.SINGLES:
        return RatingFormat.SINGLES
    if format != MatchFormat.DOUBLES:
        raise ValueError(_UNKNOWN_FORMAT)
    if divisions and all(division == _MENS for division in divisions):
        return RatingFormat.DOUBLES_MEN
    if divisions and all(division == _WOMENS for division in divisions):
        return RatingFormat.DOUBLES_WOMEN
    if all(sorted(team) == [_MENS, _WOMENS] for team in (team_a, team_b)):
        return RatingFormat.MIXED_DOUBLES
    return RatingFormat.DOUBLES


def _stored_rating_format(
    format: str, keys_a: Sequence[int], keys_b: Sequence[int]
) -> RatingFormat:
    """The rating format from the players' divisions as stored now, in one query.

    A player whose row is gone gets the format's base value here, so the
    model rules that run next (``_clean_all``) report the missing player
    as a ``MatchValidationError``, as they would without this lookup.
    """
    stored = dict(
        Player.objects.filter(pk__in=[*keys_a, *keys_b]).values_list(
            "pk", "gender_division"
        )
    )
    if any(key not in stored for key in (*keys_a, *keys_b)):
        return RatingFormat(format)
    return derive_rating_format(
        format, [stored[key] for key in keys_a], [stored[key] for key in keys_b]
    )


@dataclass(frozen=True)
class MatchIssue:
    """One problem with a submission.

    From ``validate_match``, ``field`` is ``format``, ``team_a``,
    ``team_b``, ``games``, ``games.<i>`` (0-based), ``played_on``,
    ``best_of``, ``game_target``, or ``scoring_type``. ``record_match``
    also reports the model's rules: a ``Match`` field's name (such as
    ``external_id``, ``played_on``, or ``submitted_by_user``), ``match`` for
    a rule on the whole Match, ``games.<i>`` for a game's field, and
    ``team_a`` or ``team_b`` for a participant's.
    """

    field: str
    code: str
    message: str


class MatchValidationError(Exception):
    """A submission broke one or more rules. ``issues`` is never empty."""

    def __init__(self, issues: Sequence[MatchIssue]) -> None:
        super().__init__("The match is not valid.")
        self.issues: tuple[MatchIssue, ...] = tuple(issues)


@dataclass(frozen=True)
class ValidatedMatch:
    """A submission that passed, with the defaults and inference applied."""

    format: MatchFormat
    team_a: tuple[Hashable, ...]
    team_b: tuple[Hashable, ...]
    games: tuple[tuple[int, int], ...]
    played_on: date
    best_of: BestOf
    game_target: GameTarget
    scoring_type: ScoringType


def _or_list(items: Sequence[object]) -> str:
    words = [str(item) for item in items]
    if len(words) <= 2:
        return " or ".join(words)
    return f"{', '.join(words[:-1])}, or {words[-1]}"


def _labels(choices: Sequence[str]) -> str:
    return _or_list([label.lower() for label in choices])


_FORMAT_CHOICE = f"Choose a format of {_labels(MatchFormat.labels)}."
_SCORING_CHOICE = f"Choose a scoring type of {_labels(ScoringType.labels)}."
_TARGET_CHOICE = f"Choose a game target of {_or_list(GameTarget.values)}."
_BEST_OF_CHOICE = f"Choose a best-of of {_or_list(BestOf.values)}."
_CANNOT_INFER = f"The games do not decide a best of {_or_list(BestOf.values)} match."


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _deciding_game(winners: Sequence[Team], best_of: int) -> int | None:
    """The index of the game that decides the match, or None if none does."""
    needed = best_of // 2 + 1
    wins = {Team.A: 0, Team.B: 0}
    for index, winner in enumerate(winners):
        wins[winner] += 1
        if wins[winner] == needed:
            return index
    return None


def infer_best_of(winners: Sequence[Team]) -> BestOf | None:
    """The smallest best-of the games decide, with no game after the decision.

    ``winners`` is each game's winning team in order. Returns None when no
    allowed best-of fits.
    """
    if not winners:
        return None
    for best_of in sorted(BestOf):
        if _deciding_game(winners, best_of) == len(winners) - 1:
            return best_of
    return None


def _team_players(
    field: str, label: str, team: object
) -> tuple[MatchIssue | None, tuple[Hashable, ...] | None]:
    """A team's players, or the issue that makes them unusable.

    A team is a list or tuple. A string or bytes is refused, not read one
    player per character. Each player is hashable and not None.
    """
    if not isinstance(team, list | tuple):
        return MatchIssue(
            field, "invalid", f"Enter {label} as a list of players."
        ), None
    for player in team:
        try:
            hash(player)
        except TypeError:
            player = None
        if player is None:
            return MatchIssue(
                field, "invalid_player", f"{label} lists a player that is not valid."
            ), None
    return None, tuple(team)


def _team_issues(
    match_format: MatchFormat | None, team_a: object, team_b: object
) -> tuple[list[MatchIssue], tuple[Hashable, ...] | None, tuple[Hashable, ...] | None]:
    """Check both teams. Returns the issues and each usable team, or None."""
    issues: list[MatchIssue] = []
    teams = (("team_a", "Team A", team_a), ("team_b", "Team B", team_b))
    players: dict[str, tuple[Hashable, ...] | None] = {}
    for field, label, team in teams:
        issue, players[field] = _team_players(field, label, team)
        if issue is not None:
            issues.append(issue)
    a, b = players["team_a"], players["team_b"]
    # A Player never equals its code, so the equality checks below cannot
    # be trusted unless every reference is the same kind.
    mixed = (
        a is not None
        and b is not None
        and len({type(player) for player in (*a, *b)}) > 1
    )

    for field, label, _team in teams:
        members = players[field]
        if members is None:
            continue
        if match_format is not None:
            size = PLAYERS_PER_TEAM[match_format]
            if len(members) != size:
                noun = "player" if size == 1 else "players"
                issues.append(
                    MatchIssue(
                        field,
                        "team_size",
                        f"{label} must have {size} {noun} in "
                        f"{match_format.label.lower()}.",
                    )
                )
        if not mixed and len(set(members)) != len(members):
            issues.append(
                MatchIssue(
                    field, "duplicate_player", f"{label} lists a player more than once."
                )
            )
    if mixed:
        issues.append(
            MatchIssue(
                "team_b",
                "mixed_player_references",
                "Give every player the same kind of reference.",
            )
        )
    elif a is not None and b is not None and set(a) & set(b):
        issues.append(
            MatchIssue(
                "team_b", "player_on_both_teams", "A player cannot be on both teams."
            )
        )
    return issues, a, b


def _game_issue(
    index: int, game: object, target: GameTarget | None
) -> tuple[MatchIssue | None, tuple[int, int] | None]:
    """Check one game. Returns its issue, or None and its scores."""
    field = f"games.{index}"
    number = index + 1
    if not isinstance(game, tuple | list) or len(game) != 2:
        return MatchIssue(
            field, "invalid_score", f"Game {number}: enter two scores as whole numbers."
        ), None
    a, b = game
    if not (_is_int(a) and _is_int(b)):
        return MatchIssue(
            field,
            "invalid_score",
            f"Game {number}: enter each score as a whole number.",
        ), None
    if a < 0 or b < 0:
        return MatchIssue(
            field, "negative_score", f"Game {number}: scores cannot be negative."
        ), None
    if a == b:
        return MatchIssue(field, "tie", f"Game {number} cannot end in a tie."), None
    if target is not None:
        high, low = max(a, b), min(a, b)
        if high < target:
            return MatchIssue(
                field,
                "target_not_reached",
                f"Game {number}: the winner must reach the game target.",
            ), None
        if high == target and high - low < WIN_MARGIN:
            return MatchIssue(
                field,
                "not_won_by_two",
                f"Game {number}: the winner must win by {WIN_MARGIN} points.",
            ), None
        if high > target and high - low != WIN_MARGIN:
            return MatchIssue(
                field,
                "played_past_target",
                f"Game {number}: a game played past the target ends when one "
                f"team leads by {WIN_MARGIN}.",
            ), None
    return None, (a, b)


def _played_on_issue(played_on: object) -> MatchIssue | None:
    if not isinstance(played_on, date) or isinstance(played_on, datetime):
        return MatchIssue("played_on", "invalid", "Enter the date played as a date.")
    # The latest local date anywhere on Earth right now.
    latest = (
        timezone.now().astimezone(UTC) + timedelta(minutes=MAX_UTC_OFFSET_MINUTES)
    ).date()
    if played_on > latest:
        return MatchIssue(
            "played_on", "future_date", "The date played cannot be in the future."
        )
    return None


def validate_match(
    *,
    format: str,
    team_a: Sequence[Hashable],
    team_b: Sequence[Hashable],
    games: Sequence[tuple[int, int]],
    played_on: date,
    best_of: int | None = None,
    game_target: int | None = None,
    scoring_type: str | None = None,
) -> ValidatedMatch:
    """Check a submitted match against every rule and return it with defaults.

    Raises ``MatchValidationError`` with every issue found.
    """
    issues: list[MatchIssue] = []

    match_format: MatchFormat | None = None
    if format in MatchFormat.values:
        match_format = MatchFormat(format)
    else:
        issues.append(MatchIssue("format", "invalid_choice", _FORMAT_CHOICE))
    team_issues, players_a, players_b = _team_issues(match_format, team_a, team_b)
    issues.extend(team_issues)

    scoring: ScoringType | None = ScoringType.SIDE_OUT
    if scoring_type is not None:
        if isinstance(scoring_type, str) and scoring_type in ScoringType.values:
            scoring = ScoringType(scoring_type)
        else:
            scoring = None
            issues.append(MatchIssue("scoring_type", "invalid_choice", _SCORING_CHOICE))

    target: GameTarget | None = GameTarget.ELEVEN
    if game_target is not None:
        if _is_int(game_target) and game_target in GameTarget.values:
            target = GameTarget(game_target)
        else:
            target = None
            issues.append(MatchIssue("game_target", "invalid_choice", _TARGET_CHOICE))

    chosen_best_of: BestOf | None = None
    best_of_valid = True
    if best_of is not None:
        if _is_int(best_of) and best_of in BestOf.values:
            chosen_best_of = BestOf(best_of)
        else:
            best_of_valid = False
            issues.append(MatchIssue("best_of", "invalid_choice", _BEST_OF_CHOICE))

    scores: list[tuple[int, int]] = []
    games_valid = True
    if not isinstance(games, list | tuple):
        # A string, a generator, or a dict is not read as games.
        games_valid = False
        issues.append(
            MatchIssue("games", "invalid", "Enter the games as a list of scores.")
        )
    elif len(games) == 0:
        games_valid = False
        issues.append(MatchIssue("games", "no_games", "Enter at least one game."))
    elif len(games) > MAX_GAMES:
        # One issue, not one per game of an oversized list.
        games_valid = False
        issues.append(
            MatchIssue("games", "too_many_games", f"Enter at most {MAX_GAMES} games.")
        )
    else:
        for index, game in enumerate(games):
            issue, score = _game_issue(index, game, target)
            if issue is not None:
                games_valid = False
                issues.append(issue)
            elif score is not None:
                scores.append(score)
    if target is None:
        # Without a valid target the games' outcome cannot be trusted.
        games_valid = False

    if games_valid and best_of_valid:
        winners = [Team.A if a > b else Team.B for a, b in scores]
        if chosen_best_of is None:
            chosen_best_of = infer_best_of(winners)
            if chosen_best_of is None:
                issues.append(MatchIssue("best_of", "cannot_infer", _CANNOT_INFER))
        else:
            deciding = _deciding_game(winners, chosen_best_of)
            if deciding is None:
                issues.append(
                    MatchIssue(
                        "games", "not_decided", "The games do not decide the match."
                    )
                )
            elif deciding < len(winners) - 1:
                after = deciding + 1
                issues.append(
                    MatchIssue(
                        f"games.{after}",
                        "game_after_decided",
                        f"Game {after + 1} was played after the match was decided.",
                    )
                )

    played_on_issue = _played_on_issue(played_on)
    if played_on_issue is not None:
        issues.append(played_on_issue)

    if (
        issues
        or match_format is None
        or players_a is None
        or players_b is None
        or scoring is None
        or target is None
        or chosen_best_of is None
    ):
        # Each None above always comes with an issue.
        raise MatchValidationError(issues)
    return ValidatedMatch(
        format=match_format,
        team_a=players_a,
        team_b=players_b,
        games=tuple(scores),
        played_on=played_on,
        best_of=chosen_best_of,
        game_target=target,
        scoring_type=scoring,
    )


# Duplicate detection.

_FINGERPRINT_VERSION = 1


def _canonical_teams(
    team_a: Sequence[int], team_b: Sequence[int]
) -> tuple[tuple[int, ...], tuple[int, ...], bool]:
    """Each team sorted, the lower team first, and whether they were swapped."""
    first, second = tuple(sorted(team_a)), tuple(sorted(team_b))
    if first > second:
        return second, first, True
    return first, second, False


def _digest(payload: dict[str, object]) -> bytes:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(text.encode()).digest()


def match_fingerprint(
    team_a: Sequence[int],
    team_b: Sequence[int],
    played_on: date,
    games: Sequence[Sequence[int]],
) -> str:
    """A SHA-256 hex digest of the teams, the date played, and the scores.

    Players are Player primary keys. Neither the team order (with each
    game's scores swapped to match) nor the player order within a team
    changes it. Any other change to the players, date, or scores does.
    """
    first, second, swapped = _canonical_teams(team_a, team_b)
    scores = [[b, a] if swapped else [a, b] for a, b in games]
    payload: dict[str, object] = {
        "v": _FINGERPRINT_VERSION,
        "teams": [list(first), list(second)],
        "played_on": played_on.isoformat(),
        "games": scores,
    }
    return _digest(payload).hex()


def _lock_key(payload: dict[str, object]) -> int:
    """A signed 64-bit advisory lock key for a payload."""
    return int.from_bytes(_digest(payload)[:8], "big", signed=True)


def _lineup_lock_key(
    team_a: Sequence[int], team_b: Sequence[int], played_on: date
) -> int:
    """One lock for every exact and near duplicate of a lineup on a date."""
    first, second, _swapped = _canonical_teams(team_a, team_b)
    return _lock_key(
        {
            "lock": "lineup",
            "teams": [list(first), list(second)],
            "played_on": played_on.isoformat(),
        }
    )


def _external_id_lock_key(application_id: int, external_id: str) -> int:
    """One lock for an application's external id."""
    return _lock_key(
        {"lock": "external_id", "application": application_id, "id": external_id}
    )


_FINGERPRINT_CONSTRAINT = "matches_match_fingerprint_unique_unless_voided"
_EXTERNAL_ID_CONSTRAINT = "matches_match_external_id_unique_per_application"


class DuplicateKind(StrEnum):
    """Why a submission was refused as a duplicate."""

    DUPLICATE_MATCH = "duplicate_match"
    DUPLICATE_EXTERNAL_ID = "duplicate_external_id"


_DUPLICATE_MESSAGES: dict[DuplicateKind, str] = {
    DuplicateKind.DUPLICATE_MATCH: "This match was already submitted",
    DuplicateKind.DUPLICATE_EXTERNAL_ID: (
        "This application already submitted a match with this external ID"
    ),
}
_CODE_JOINERS: dict[DuplicateKind, str] = {
    DuplicateKind.DUPLICATE_MATCH: " as ",
    DuplicateKind.DUPLICATE_EXTERNAL_ID: ": ",
}


class DuplicateMatchError(Exception):
    """A submission repeats an earlier Match.

    ``existing_code`` is that Match's OPEN code when the submitter may know
    it (see ``_visible_code``), otherwise None. The message is fixed text
    and that code, never a submitted value.
    """

    def __init__(self, kind: DuplicateKind, existing_code: str | None) -> None:
        self.kind = kind
        self.existing_code = existing_code
        super().__init__(self.message)

    @property
    def message(self) -> str:
        text = _DUPLICATE_MESSAGES[self.kind]
        if self.existing_code is None:
            return f"{text}."
        return f"{text}{_CODE_JOINERS[self.kind]}{self.existing_code}."


def _visible_code(
    existing: Match,
    submitted_by_user: "_User | None",
    submitted_by_application: "PartnerApplication | None",
) -> str | None:
    """The earlier Match's code, if the submitter may know it, else None.

    The submitter may know it when it submitted that Match (the same
    Account or Partner Application), or when the submitting Account's
    Player played in it. Anyone else learns only that such a match exists.
    """
    if (
        submitted_by_application is not None
        and existing.submitted_by_application_id == submitted_by_application.pk
    ):
        return existing.public_code
    if submitted_by_user is not None and (
        existing.submitted_by_user_id == submitted_by_user.pk
        or existing.participants.filter(player__account=submitted_by_user).exists()
    ):
        return existing.public_code
    return None


def _issues_from_validation_error(error: ValidationError) -> list[MatchIssue]:
    """One ``MatchIssue`` per model validation error. Non-field errors go
    under ``match``."""
    issues: list[MatchIssue] = []
    for name, items in error.error_dict.items():
        field = "match" if name == NON_FIELD_ERRORS else name
        for item in items:
            for message in item.messages:
                issues.append(MatchIssue(field, item.code or "invalid", message))
    return issues


def _advisory_lock(key: int) -> None:
    """Hold a Postgres advisory lock until the transaction ends."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def advisory_lock(key: int) -> None:
    """Wait for and hold an advisory lock until the transaction ends."""
    _advisory_lock(key)


def try_advisory_lock(key: int) -> bool:
    """Hold an advisory lock until the transaction ends, if free now.

    Returns False, without waiting, when another session holds it.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_xact_lock(%s)", [key])
        row = cursor.fetchone()
    return bool(row and row[0])


def _external_id_match(
    application: "PartnerApplication", external_id: str
) -> Match | None:
    """The application's Match with this external id, in any status.

    A voided Match keeps its external id, and its correction (#26) reuses
    it, so the Match that is not voided comes first, else the latest
    voided one.
    """
    matches = Match.objects.filter(
        submitted_by_application=application, external_id=external_id
    )
    live = matches.exclude(status=MatchStatus.VOIDED).first()
    if live is not None:
        return live
    return matches.order_by("-created_at", "-pk").first()


def _exact_match(fingerprint: str) -> Match | None:
    return (
        Match.objects.filter(fingerprint=fingerprint)
        .exclude(status=MatchStatus.VOIDED)
        .order_by("created_at", "pk")
        .first()
    )


def _near_matches(
    team_a: Sequence[int], team_b: Sequence[int], played_on: date
) -> list[Match]:
    """Matches that are not voided, with the same two teams and date, in any order.

    Step ``_exact_match`` already ruled out equal fingerprints, so these
    differ in scores, or have no fingerprint (a row made outside
    ``record_match``).
    """
    lineup = {frozenset(team_a), frozenset(team_b)}
    candidates = (
        Match.objects.filter(played_on=played_on, participants__player_id=team_a[0])
        .exclude(status=MatchStatus.VOIDED)
        .distinct()
        .order_by("created_at", "pk")
        .prefetch_related("participants")
    )
    near: list[Match] = []
    for candidate in candidates:
        teams: dict[str, set[int]] = {Team.A: set(), Team.B: set()}
        for participant in candidate.participants.all():
            teams.setdefault(participant.team, set()).add(participant.player_id)
        if {frozenset(members) for members in teams.values()} == lineup:
            near.append(candidate)
    return near


def _player_keys(team: object) -> list[int]:
    """Each team member's pk. A member that is not a saved Player is a
    programming error, not user input."""
    if not isinstance(team, list | tuple):
        # validate_match reports it.
        return []
    keys: list[int] = []
    for member in team:
        if not isinstance(member, Player) or member.pk is None:
            raise TypeError("record_match takes saved Players as team members.")
        keys.append(member.pk)
    return keys


def _clean_all(
    match: Match, participants: Sequence[MatchParticipant], games: Sequence[Game]
) -> None:
    """Run every model's field and clean() rules, before anything is written.

    Raises one ``MatchValidationError`` with every issue. The unique
    constraints are left to the duplicate checks and the database: a
    conflict that slips past the checks becomes ``DuplicateMatchError`` on
    insert. A game's issues go under ``games.<i>`` and a participant's
    under its team's field.
    """
    issues: list[MatchIssue] = []
    try:
        match.full_clean(validate_constraints=False)
    except ValidationError as error:
        issues.extend(_issues_from_validation_error(error))
    for participant in participants:
        field = "team_a" if participant.team == Team.A else "team_b"
        try:
            participant.full_clean(exclude=["match"], validate_constraints=False)
        except ValidationError as error:
            issues.extend(
                MatchIssue(field, issue.code, issue.message)
                for issue in _issues_from_validation_error(error)
            )
    for index, game in enumerate(games):
        try:
            game.full_clean(exclude=["match"], validate_constraints=False)
        except ValidationError as error:
            issues.extend(
                MatchIssue(f"games.{index}", issue.code, issue.message)
                for issue in _issues_from_validation_error(error)
            )
    if issues:
        raise MatchValidationError(issues)


def _save_new(match: Match) -> None:
    """Insert the Match, turning a duplicate constraint into DuplicateMatchError.

    The pre-insert checks run under advisory locks, so this fires only when
    a write skipped them.
    """
    try:
        with transaction.atomic():
            match.save()
    except IntegrityError as error:
        constraint = getattr(
            getattr(error.__cause__, "diag", None), "constraint_name", None
        )
        existing: Match | None = None
        kind = DuplicateKind.DUPLICATE_MATCH
        if constraint == _EXTERNAL_ID_CONSTRAINT and match.submitted_by_application:
            kind = DuplicateKind.DUPLICATE_EXTERNAL_ID
            existing = _external_id_match(
                match.submitted_by_application, match.external_id
            )
        elif constraint == _FINGERPRINT_CONSTRAINT:
            existing = _exact_match(match.fingerprint)
        if existing is None:
            raise
        code = _visible_code(
            existing, match.submitted_by_user, match.submitted_by_application
        )
        raise DuplicateMatchError(kind, code) from None


@dataclass(frozen=True)
class RecordedMatch:
    """What ``record_match`` did.

    ``created`` is True for a new Match, and False when the submission
    confirmed an existing pending Match it exactly duplicates (see
    ``record_match``).
    """

    match: Match
    created: bool


@dataclass(frozen=True)
class MatchDraft:
    """A validated, cleaned Match with its participants and games, not saved.

    Built by ``draft_match`` and written by ``insert_draft``, the creation
    core that ``record_match`` and the workflow's ``edit_match`` share.
    """

    match: Match
    participants: tuple[MatchParticipant, ...]
    games: tuple[Game, ...]
    keys_a: tuple[int, ...]
    keys_b: tuple[int, ...]


def draft_match(
    *,
    format: str,
    team_a: Sequence[Player],
    team_b: Sequence[Player],
    games: Sequence[tuple[int, int]],
    played_on: date,
    best_of: int | None = None,
    game_target: int | None = None,
    scoring_type: str | None = None,
    submitted_by_user: "_User | None" = None,
    submitted_by_application: "PartnerApplication | None" = None,
    external_id: str = "",
    event: "Event | None" = None,
    club: "Club | None" = None,
    played_at: datetime | None = None,
    played_at_utc_offset: int | None = None,
    location: str = "",
    event_label: str = "",
    bracket_label: str = "",
    sequence: int = 0,
    submission_batch: SubmissionBatch | None = None,
) -> MatchDraft:
    """Validate a match and run every model rule, with no lock or write.

    Raises ``MatchValidationError`` for a score or model rule. Checks no
    permission and does not check ``submission_batch``: each caller checks
    its own first (``record_match`` calls ``check_batch``).
    """
    keys_a = _player_keys(team_a)
    keys_b = _player_keys(team_b)
    validated = validate_match(
        format=format,
        team_a=team_a,
        team_b=team_b,
        games=games,
        played_on=played_on,
        best_of=best_of,
        game_target=game_target,
        scoring_type=scoring_type,
    )
    fingerprint = match_fingerprint(
        keys_a, keys_b, validated.played_on, validated.games
    )
    match = Match(
        format=validated.format,
        rating_format=_stored_rating_format(validated.format, keys_a, keys_b),
        played_on=validated.played_on,
        best_of=validated.best_of,
        game_target=validated.game_target,
        scoring_type=validated.scoring_type,
        submitted_by_user=submitted_by_user,
        submitted_by_application=submitted_by_application,
        external_id=external_id,
        event=event,
        club=club,
        played_at=played_at,
        played_at_utc_offset=played_at_utc_offset,
        location=location,
        event_label=event_label,
        bracket_label=bracket_label,
        sequence=sequence,
        fingerprint=fingerprint,
        submission_batch=submission_batch,
    )
    participants = [
        MatchParticipant(match=match, player_id=key, team=team)
        for team, keys in ((Team.A, keys_a), (Team.B, keys_b))
        for key in keys
    ]
    game_rows = [
        Game(match=match, game_number=number, team_a_score=a, team_b_score=b)
        for number, (a, b) in enumerate(validated.games, start=1)
    ]
    # Every model rule runs before any lock, lookup, or write. Among other
    # things this strips external_id, so the lookups use the stored form.
    _clean_all(match, participants, game_rows)
    return MatchDraft(
        match=match,
        participants=tuple(participants),
        games=tuple(game_rows),
        keys_a=tuple(keys_a),
        keys_b=tuple(keys_b),
    )


def stored_lineup_lock_key(match: Match) -> int:
    """The lineup lock key of a saved match, from its stored players and date."""
    played_on = Match.objects.filter(pk=match.pk).values_list("played_on", flat=True)
    teams: dict[str, list[int]] = {Team.A: [], Team.B: []}
    for player_id, team in MatchParticipant.objects.filter(
        match_id=match.pk
    ).values_list("player_id", "team"):
        teams.setdefault(team, []).append(player_id)
    return _lineup_lock_key(teams[Team.A], teams[Team.B], played_on.get())


def lock_draft(draft: MatchDraft, *, also: Sequence[int] = ()) -> None:
    """Take the draft's advisory locks: its external id's, then the lineups'.

    The lineup locks are the draft's and any in ``also`` (an edit passes
    the old match's), without repeats and in key order. Call inside a
    transaction. The locks last until the outermost transaction ends.
    Take them before any row lock, as every path does, so a row lock never
    waits behind an advisory lock in one path and the other way round in
    another.
    """
    match = draft.match
    application = match.submitted_by_application
    if match.external_id and application is not None:
        _advisory_lock(_external_id_lock_key(application.pk, match.external_id))
    lineup = _lineup_lock_key(draft.keys_a, draft.keys_b, match.played_on)
    for key in sorted({lineup, *also}):
        _advisory_lock(key)


def check_external_id(draft: MatchDraft, *, replaces: Match | None = None) -> None:
    """Refuse an external id its application already used, in any status.

    ``replaces`` is the voided Match an edit corrects, whose external id
    the correction keeps. Call after ``lock_draft``.
    """
    match = draft.match
    application = match.submitted_by_application
    if not match.external_id or application is None:
        return
    existing = _external_id_match(application, match.external_id)
    if existing is not None and (replaces is None or existing.pk != replaces.pk):
        # The same application submitted it, so the code is named.
        raise DuplicateMatchError(
            DuplicateKind.DUPLICATE_EXTERNAL_ID, existing.public_code
        )


def exact_duplicate(draft: MatchDraft) -> Match | None:
    """The Match that is not voided with the draft's fingerprint, if any.

    Call after ``lock_draft``.
    """
    return _exact_match(draft.match.fingerprint)


def near_duplicates(draft: MatchDraft) -> list[Match]:
    """Matches that are not voided with the draft's lineup and date.

    Call after ``lock_draft``.
    """
    return _near_matches(draft.keys_a, draft.keys_b, draft.match.played_on)


def duplicate_error(
    existing: Match, viewer: "Actor | None", *, reveal: bool = False
) -> DuplicateMatchError:
    """The error for an exact duplicate of ``existing``.

    It names the code when ``viewer`` (the Account or application that
    acted: the submitter, or an edit's editor) may know it, by
    ``_visible_code``'s rules, or when ``reveal`` is set (a moderator).
    """
    if reveal:
        code: str | None = existing.public_code
    else:
        user = viewer if isinstance(viewer, get_user_model()) else None
        application = viewer if isinstance(viewer, get_application_model()) else None
        code = _visible_code(existing, user, application)
    return DuplicateMatchError(DuplicateKind.DUPLICATE_MATCH, code)


def insert_draft(
    draft: MatchDraft, *, status: MatchStatus, corrects: Match | None = None
) -> tuple[Match, list[Match]]:
    """Write the draft's Match, participants, games, and flags.

    Call after ``lock_draft`` and the duplicate checks, in the same
    transaction. Returns the new Match and the earlier near duplicates that
    were counting before it was written: each is now held by its flag.
    """
    match = draft.match
    near = _near_matches(draft.keys_a, draft.keys_b, match.played_on)
    was_counting = [earlier for earlier in near if earlier.is_counted()]
    match.status = status
    match.corrects = corrects
    _save_new(match)
    # Set the links again now that the Match has its primary key.
    for participant in draft.participants:
        participant.match = match
    for game in draft.games:
        game.match = match
    MatchParticipant.objects.bulk_create(draft.participants)
    Game.objects.bulk_create(draft.games)
    MatchFlag.objects.bulk_create(
        [
            MatchFlag(
                match=match,
                reason=FlagReason.POSSIBLE_DUPLICATE,
                duplicate_of=earlier,
            )
            for earlier in near
        ]
    )
    return match, was_counting


def audit_actor(actor: "Actor | None") -> dict[str, Any]:
    """``record_event`` keyword arguments naming ``actor`` as who acted."""
    if isinstance(actor, get_application_model()):
        return {"actor_application": actor}
    return {"actor": actor}


def send_holds(held: Sequence[Match]) -> None:
    """Send ``match_changed(change=hold)`` for each match now held."""
    for earlier in held:
        match_changed.send(
            sender=Match,
            match=earlier,
            change=MatchChange.HOLD,
            earliest_date=earlier.played_on,
        )


def _confirmed_by(match: Match, actor: "Actor") -> bool:
    """True when ``actor`` moved ``match`` from pending to confirmed.

    That is a ``match.confirm`` Audit Event on ``match`` naming ``actor``,
    other than the one ``record_match`` writes when it creates a match
    confirmed (``before`` is empty there): a plain repeat of a submission
    that created the match is still refused with its code (ADR 0011).
    """
    events = AuditEvent.objects.filter(
        action=AuditAction.MATCH_CONFIRM,
        target_content_type=ContentType.objects.get_for_model(Match),
        target_object_id=str(match.pk),
        before__isnull=False,
    )
    if isinstance(actor, get_application_model()):
        return events.filter(actor_application_id=actor.pk).exists()
    return events.filter(actor_user_id=actor.pk).exists()


def _confirm_pending_duplicate(existing: Match, actor: "Actor") -> Match | None:
    """Confirm ``existing`` if it is still pending, under a row lock.

    Returns the confirmed Match, or None when it is no longer pending. A
    confirmed Match that ``actor`` itself confirmed (a retry of the same
    submission) is returned unchanged, with no new Audit Event or signal.
    """
    locked = Match.objects.select_for_update().get(pk=existing.pk)
    if locked.status == MatchStatus.CONFIRMED and _confirmed_by(locked, actor):
        return locked
    if locked.status != MatchStatus.PENDING:
        return None
    before = snapshot(locked, ["status"])
    locked.status = MatchStatus.CONFIRMED
    locked.save(update_fields=["status"])
    record_event(
        action=AuditAction.MATCH_CONFIRM,
        target=locked,
        before=before,
        after=snapshot(locked, ["status"]),
        **audit_actor(actor),
    )
    match_confirmed.send(sender=Match, match=locked)
    return locked


def check_batch(
    batch: SubmissionBatch,
    *,
    submitted_by_user: "_User | None",
    submitted_by_application: "PartnerApplication | None",
    event: "Event | None",
    club: "Club | None",
) -> None:
    """Refuse a batch that does not fit the submission, with fixed text.

    Raises ``ValueError`` (``BATCH_REFUSED``) for a batch with no stored
    row, a batch whose stored submitter is not exactly this submission's
    (the same Account, or the same application, and no other), a batch
    whose stored Event is set and is not the match's, or one whose stored
    Club is set and is not the match's. A batch with no Event or no Club
    accepts any value there. The batch's values are read from the
    database by its id, never from the instance, so a batch changed in
    memory cannot link a match into another submitter's batch.
    """
    user_id = submitted_by_user.pk if submitted_by_user is not None else None
    application_id = (
        submitted_by_application.pk if submitted_by_application is not None else None
    )
    event_id = event.pk if event is not None else None
    club_id = club.pk if club is not None else None
    stored = (
        SubmissionBatch.objects.filter(pk=batch.pk)
        .values(
            "submitted_by_user_id",
            "submitted_by_application_id",
            "event_id",
            "club_id",
        )
        .first()
        if batch.pk is not None
        else None
    )
    fits = (
        stored is not None
        and stored["submitted_by_user_id"] == user_id
        and stored["submitted_by_application_id"] == application_id
        and stored["event_id"] in (None, event_id)
        and stored["club_id"] in (None, club_id)
    )
    if not fits:
        raise ValueError(BATCH_REFUSED)


def record_match(
    *,
    format: str,
    team_a: Sequence[Player],
    team_b: Sequence[Player],
    games: Sequence[tuple[int, int]],
    played_on: date,
    best_of: int | None = None,
    game_target: int | None = None,
    scoring_type: str | None = None,
    submitted_by_user: "_User | None" = None,
    submitted_by_application: "PartnerApplication | None" = None,
    external_id: str = "",
    event: "Event | None" = None,
    club: "Club | None" = None,
    played_at: datetime | None = None,
    played_at_utc_offset: int | None = None,
    location: str = "",
    event_label: str = "",
    bracket_label: str = "",
    sequence: int = 0,
    submission_batch: SubmissionBatch | None = None,
) -> RecordedMatch:
    """Validate a submission, check it for duplicates, and add it to the ledger.

    Teams are saved Players. Raises ``MatchPermissionDenied`` when the
    submitter may not submit this match (checked first, before any
    validation, lookup, lock, or write), ``MatchValidationError`` for a
    score or model rule, ``DuplicateMatchError`` for an exact duplicate or
    a repeated external id, and writes nothing in any of these cases. Every
    submitter given is checked. With no submitter there is nothing to
    check, and with two permitted ones the model rules refuse the match as
    a ``MatchValidationError``. A near duplicate is
    saved with one ``possible_duplicate`` flag per earlier Match.

    ``submission_batch`` is the upload this match came in (a batch path,
    #71 or #72); a single submission leaves it empty. It is checked right
    after the permissions and before validation (``check_batch``): a
    batch that does not fit raises ``ValueError`` (``BATCH_REFUSED``) and
    nothing is written. A pending match that an exact duplicate confirms
    instead keeps its own batch link, if any.

    A match with an Event or a Club comes from a permitted non-self
    submitter (decision F4) and is created confirmed, with a
    ``match.confirm`` Audit Event naming the submitter and one
    ``match_confirmed`` signal. A self-submission (no Event, no Club,
    decision F1) is created pending. Such a submitter's exact duplicate of
    a pending Match confirms that Match the same way instead of raising,
    and returns it with ``created=False``. A retry of that submission by the
    same submitter returns the same result, with no new Audit Event or
    signal. The submission's external id is not stored on that Match, so
    the application may use it again later (accepted). An exact duplicate
    of a Match in any other status, or from a self-submitter, still
    raises. Each earlier
    match that was counting and is now held by a new flag gets
    ``match_changed`` with ``change=hold``. Signals are sent inside the
    transaction (``apps.matches.signals``).

    Call it in its own transaction, never several times inside one outer
    ``transaction.atomic()``. Its advisory locks (an external id lock, then
    a lineup lock) last until the outermost transaction ends, so two
    callers that each record several matches in one outer transaction can
    deadlock, whatever order they sort them in. A batch path (#71, #72) must
    record each match in its own transaction and report a result per
    match; #71's all-or-nothing mode must get its all-or-nothing result
    another way.
    """
    keys_a = _player_keys(team_a)
    keys_b = _player_keys(team_b)
    # Every submitter given is checked, so an unsaved second one cannot
    # skip the check. Zero, or two permitted ones, fail the model's
    # one-submitter rule in _clean_all, before anything is written.
    submission = MatchSubmission(
        event=event, club=club, player_ids=frozenset(keys_a + keys_b)
    )
    if submitted_by_user is not None:
        check_can_create_match(submitted_by_user, submission)
    if submitted_by_application is not None:
        check_can_create_match(submitted_by_application, submission)
    if submission_batch is not None:
        check_batch(
            submission_batch,
            submitted_by_user=submitted_by_user,
            submitted_by_application=submitted_by_application,
            event=event,
            club=club,
        )
    draft = draft_match(
        format=format,
        team_a=team_a,
        team_b=team_b,
        games=games,
        played_on=played_on,
        best_of=best_of,
        game_target=game_target,
        scoring_type=scoring_type,
        submitted_by_user=submitted_by_user,
        submitted_by_application=submitted_by_application,
        external_id=external_id,
        event=event,
        club=club,
        played_at=played_at,
        played_at_utc_offset=played_at_utc_offset,
        location=location,
        event_label=event_label,
        bracket_label=bracket_label,
        sequence=sequence,
        submission_batch=submission_batch,
    )
    confirms = event is not None or club is not None
    submitter: Actor | None = submitted_by_user or submitted_by_application

    with transaction.atomic():
        lock_draft(draft)
        check_external_id(draft)
        existing = exact_duplicate(draft)
        if existing is not None and confirms and submitter is not None:
            # The match may change while this call waits for its row lock
            # (a void, a confirmation). The lineup lock stops any new
            # duplicate from appearing, so one more look is enough.
            for _attempt in range(2):
                confirmed = _confirm_pending_duplicate(existing, submitter)
                if confirmed is not None:
                    return RecordedMatch(match=confirmed, created=False)
                existing = exact_duplicate(draft)
                if existing is None:
                    break
        if existing is not None:
            raise duplicate_error(existing, submitter)

        status = MatchStatus.CONFIRMED if confirms else MatchStatus.PENDING
        match, held = insert_draft(draft, status=status)
        if confirms:
            record_event(
                action=AuditAction.MATCH_CONFIRM,
                target=match,
                before=None,
                after=snapshot(match, ["status"]),
                **audit_actor(submitter),
            )
            match_confirmed.send(sender=Match, match=match)
        send_holds(held)
    return RecordedMatch(match=match, created=True)
