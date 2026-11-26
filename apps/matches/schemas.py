"""The API's request and response schemas for Matches (issue #31).

A Match in a response names each participant by display name and OPEN
Code, whatever that player's own Privacy Settings. The one exception is a
participant whose Account is inactive: ADR 0002 shows a Player's name only
while their Account is active, so they show as ``PRIVATE_PLAYER_NAME``
with no code, unless the viewer is that participant's own Account.

No schema here carries a row id, the submitter, a flag, or any personal
field other than a display name. Confirmation state shows only as values
derived for the viewer (issue #193): their own team, their own latest
decision, and, for a participant, whether each team has a confirmation.
No confirmation row is shown, and nothing names who confirmed or
disputed, though a participant can sometimes infer it (ADR 0015).
"""

import unicodedata
from collections.abc import Callable
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

from django.db.models import Prefetch, QuerySet
from ninja import Schema
from pydantic import AfterValidator, BeforeValidator, Field
from pydantic_core import PydanticCustomError

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
    ResultsFile,
    ResultsFileStatus,
    ScoringType,
    Team,
)
from apps.matches.results_files import ResultRow

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

PRIVATE_PLAYER_NAME = "Private player"

# Generous caps, well above what ``validate_match`` allows, so an ordinary
# mistake still gets its message. They only stop a huge body from making
# the view normalize and look up thousands of codes.
MAX_TEAM_ENTRIES = 10
MAX_GAME_ENTRIES = 20


def _at_most(limit: int, message: str) -> Callable[[object], object]:
    """A check that refuses a list longer than ``limit`` with fixed text.

    It runs before the items are parsed. Pydantic's own ``too_long``
    message names the list's length, which the client sent, so it is
    replaced.
    """

    def check(value: object) -> object:
        if isinstance(value, list | tuple) and len(value) > limit:
            raise PydanticCustomError("too_long", message)
        return value

    return check


TeamCodes = Annotated[
    list[str],
    BeforeValidator(
        _at_most(
            MAX_TEAM_ENTRIES, f"Enter at most {MAX_TEAM_ENTRIES} players for a team."
        )
    ),
    Field(json_schema_extra={"maxItems": MAX_TEAM_ENTRIES}),
]


class GameScoreIn(Schema):
    """One game's score: the points each team won."""

    team_a: int
    team_b: int


GameScores = Annotated[
    list[GameScoreIn],
    BeforeValidator(
        _at_most(MAX_GAME_ENTRIES, f"Enter at most {MAX_GAME_ENTRIES} games.")
    ),
    Field(json_schema_extra={"maxItems": MAX_GAME_ENTRIES}),
]


class MatchCreateIn(Schema):
    """A player's own match submission.

    The field names are ``validate_match``'s argument names, so its issues
    name the request fields. A missing game target means 11, a missing
    scoring type means side-out, and a missing best-of is inferred from
    the games. Each team takes at most ``MAX_TEAM_ENTRIES`` codes and the
    games at most ``MAX_GAME_ENTRIES`` scores.
    """

    format: MatchFormat
    played_on: date
    team_a: TeamCodes
    team_b: TeamCodes
    games: GameScores
    best_of: BestOf | None = None
    scoring_type: ScoringType | None = None
    game_target: GameTarget | None = None


class MatchCorrectionIn(MatchCreateIn):
    """A player's correction of a self-reported match (#267).

    The same fields as a submission. The teams must be the match's own two
    teams, in either order. A missing game target or scoring type is the
    corrected match's, and a missing best-of is inferred from the games.
    """


MAX_REASON_LENGTH = 1000
# Control characters and lone surrogates, as Player search refuses them.
# Postgres refuses a NUL in text, which would be a 500. A typed reason may
# hold line breaks and tabs.
_REASON_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})
_REASON_ALLOWED_CONTROLS = frozenset({"\n", "\r", "\t"})


def _reason(value: object) -> object:
    """Refuse a blank, overlong, or control-character reason with fixed text.

    The messages never repeat the value: a reason is free text, and the
    422 response returns each message to the client.
    """
    if isinstance(value, str):
        if any(
            unicodedata.category(char) in _REASON_REFUSED_CATEGORIES
            and char not in _REASON_ALLOWED_CONTROLS
            for char in value
        ):
            raise PydanticCustomError(
                "invalid", "The reason has a character not allowed."
            )
        if not value.strip():
            raise PydanticCustomError("blank", "Give a reason.")
        if len(value) > MAX_REASON_LENGTH:
            raise PydanticCustomError(
                "too_long",
                f"Enter a reason of at most {MAX_REASON_LENGTH} characters.",
            )
    return value


ReasonText = Annotated[
    str,
    AfterValidator(_reason),
    Field(json_schema_extra={"minLength": 1, "maxLength": MAX_REASON_LENGTH}),
]


class DisputeOutcome(StrEnum):
    """How the Event's Organizer settles a dispute."""

    CONFIRMED = "confirmed"
    VOIDED = "voided"


class DisputeResolutionIn(Schema):
    """The Organizer's decision on a disputed match, and why.

    The reason is stored in the match's audit log, which cannot be
    erased, so it should not name a player.
    """

    outcome: DisputeOutcome
    reason: ReasonText


class DisputeCorrectionIn(MatchCreateIn):
    """The Organizer's correction of a disputed match, and why.

    The fields are ``create_match``'s, except that a scoring type or game
    target left out is the disputed match's; a missing best-of is inferred
    from the games. The reason is stored in the match's audit log, which
    cannot be erased, so it should not name a player.
    """

    reason: ReasonText


class ParticipantOut(Schema):
    """A player in a match. ``code`` is null for a Private player."""

    code: str | None
    display_name: str


class GameOut(Schema):
    """One game's number and the points each team won."""

    number: int
    team_a: int
    team_b: int


class MyDecision(StrEnum):
    """The viewer's own latest decision on a match."""

    CONFIRM = "confirm"
    DISPUTE = "dispute"
    WITHDRAW = "withdraw"
    NONE = "none"


class TeamConfirmationsOut(Schema):
    """Whether a player on each team has confirmed the match."""

    a: bool
    b: bool


class MatchOut(Schema):
    """A match, its teams, and its games, as the signed-in Account sees it.

    ``my_team`` is the viewer's own team, null when the viewer's Player did
    not play in the match. ``my_decision`` is the latest confirm, dispute,
    or withdraw this player recorded on the match, ``none`` when they
    recorded none or did not play. A repeated decision is not recorded
    again, so confirming again after a dispute that the Event's verified
    organizer resolved still shows ``dispute``. An organizer's confirmation is not a player
    decision: a player who confirms as a member of the match's Event
    Organizer still shows ``none``. For a participant, ``confirmed_by_team``
    says whether a player on each team recorded a confirmation (a dispute is
    not a confirmation); it is null for anyone else. A confirmation still
    shows in ``confirmed_by_team`` after the same player disputes, and
    after they withdraw that dispute, since a recorded decision is never
    removed: a player who confirmed, disputed, then withdrew shows
    ``withdraw`` while their confirmation stays in ``confirmed_by_team``.
    It counts player confirmations only: a match confirmed by an organizer,
    or a correction, can be confirmed with both false, so
    read ``status`` for whether the match is confirmed. ``created_at`` is when the match was submitted.
    """

    code: str
    status: MatchStatus
    format: MatchFormat
    played_on: date
    best_of: BestOf
    scoring_type: ScoringType
    game_target: GameTarget
    team_a: list[ParticipantOut]
    team_b: list[ParticipantOut]
    games: list[GameOut]
    my_team: Team | None
    my_decision: MyDecision
    confirmed_by_team: TeamConfirmationsOut | None
    created_at: datetime


def with_match_details(queryset: QuerySet[Match]) -> QuerySet[Match]:
    """Prefetch what ``match_out`` reads, in a fixed number of queries."""
    participants = MatchParticipant.objects.select_related("player__account")
    confirmations = MatchConfirmation.objects.order_by("pk")
    return (
        queryset.prefetch_related(
            Prefetch("participants", queryset=participants.order_by("pk"))
        )
        .prefetch_related(
            Prefetch("games", queryset=Game.objects.order_by("game_number"))
        )
        .prefetch_related(Prefetch("confirmations", queryset=confirmations))
    )


def _participant_out(
    participant: MatchParticipant, viewer: "_User | None"
) -> ParticipantOut:
    player = participant.player
    is_viewer = viewer is not None and viewer.pk == player.account_id
    if not player.account.is_active and not is_viewer:
        return ParticipantOut(code=None, display_name=PRIVATE_PLAYER_NAME)
    return ParticipantOut(code=player.public_code, display_name=player.display_name)


def _viewer_fields(
    participants: list[MatchParticipant],
    confirmations: list[MatchConfirmation],
    viewer: "_User | None",
) -> tuple[Team | None, MyDecision, TeamConfirmationsOut | None]:
    """The viewer's team, latest decision, and each team's confirmation.

    A viewer whose Player did not play in the match gets no team, no
    decision, and no team confirmations. The confirmations are in
    insertion order, so the viewer's last row is their latest decision.
    """
    own = next(
        (
            p
            for p in participants
            if viewer is not None and p.player.account_id == viewer.pk
        ),
        None,
    )
    if own is None:
        return None, MyDecision.NONE, None
    decision = MyDecision.NONE
    for row in confirmations:
        if row.player_id == own.player_id:
            decision = MyDecision(row.decision)
    team_of = {p.player_id: p.team for p in participants}
    confirmed = {
        team_of.get(row.player_id)
        for row in confirmations
        if row.decision == ConfirmationDecision.CONFIRM
    }
    return (
        Team(own.team),
        decision,
        TeamConfirmationsOut(a=Team.A in confirmed, b=Team.B in confirmed),
    )


def match_out(match: Match, viewer: "_User | None") -> MatchOut:
    """The response for ``match`` as ``viewer`` sees it.

    Reads the participants, their players and Accounts, the games, and the
    confirmations, so load the match through ``with_match_details`` first.
    """
    participants = list(match.participants.all())
    my_team, my_decision, confirmed_by_team = _viewer_fields(
        participants, list(match.confirmations.all()), viewer
    )
    return MatchOut(
        code=match.public_code,
        status=MatchStatus(match.status),
        format=MatchFormat(match.format),
        played_on=match.played_on,
        best_of=BestOf(match.best_of),
        scoring_type=ScoringType(match.scoring_type),
        game_target=GameTarget(match.game_target),
        team_a=[_participant_out(p, viewer) for p in participants if p.team == Team.A],
        team_b=[_participant_out(p, viewer) for p in participants if p.team == Team.B],
        games=[
            GameOut(number=g.game_number, team_a=g.team_a_score, team_b=g.team_b_score)
            for g in match.games.all()
        ],
        my_team=my_team,
        my_decision=my_decision,
        confirmed_by_team=confirmed_by_team,
        created_at=match.created_at,
    )


class HoldOut(Schema):
    """A held match the signed-in Account may vouch for (#270, ADR 0034).

    ``match`` is the match as the Account sees it. The other match of each
    possible duplicate flag that holds it is never named. ``vouched`` is
    true when the Account's own level has vouched for this match on every
    open flag that holds it: for an Event match, its organizer; for a
    match with no Event, a player on the Account's team.
    """

    match: MatchOut
    vouched: bool


# CSV results files (#71)


class ResultsIssueOut(Schema):
    """One issue or warning on a row: the columns it concerns (canonical
    names such as ``playerB1Id``, empty for the whole row), a code, and a
    fixed message. Never a cell value."""

    columns: list[str]
    code: str
    message: str


class ResultsRowOut(Schema):
    """One match row's result. ``row`` is the CSV record number and
    ``sequence`` the row's place among the match rows. ``match`` is the
    Match's OPEN Code: the recorded match, the row's match that was voided
    later (issue code ``voided``), or, for a duplicate, the earlier match
    when the uploader may know it."""

    row: int
    sequence: int
    result: ResultRow
    match: str | None
    issues: list[ResultsIssueOut]
    warnings: list[ResultsIssueOut]


class ResultsFileOut(Schema):
    """An uploaded results file and its report.

    ``status`` is ``pending``, ``done``, or ``failed``. ``error`` is the
    code of a file that failed as a whole (``not_utf8``, ``malformed``,
    ``bad_header``, ``no_rows``, ``too_many_rows``, ``expired``, or
    ``voided``: a moderator voided the upload's matches), else null.
    ``rows`` holds the rows processed so far: a file that failed partway
    keeps them.
    """

    code: str
    event: str
    status: ResultsFileStatus
    error: str | None
    game_target: int
    location: str
    created_at: datetime
    processed_at: datetime | None
    recorded: int
    duplicates: int
    failed: int
    rows: list[ResultsRowOut]


def results_file_out(results_file: ResultsFile) -> ResultsFileOut:
    """``results_file`` for the API. Load it with its batch's Event."""
    rows = [ResultsRowOut.model_validate(row) for row in results_file.report]

    def count(result: ResultRow) -> int:
        return sum(1 for row in rows if row.result == result)

    event = results_file.submission_batch.event
    return ResultsFileOut(
        code=results_file.public_code,
        event=event.public_code if event is not None else "",
        status=ResultsFileStatus(results_file.status),
        error=results_file.error_code or None,
        game_target=results_file.game_target,
        location=results_file.location,
        created_at=results_file.created_at,
        processed_at=results_file.processed_at,
        recorded=count(ResultRow.RECORDED),
        duplicates=count(ResultRow.DUPLICATE),
        failed=count(ResultRow.FAILED),
        rows=rows,
    )
