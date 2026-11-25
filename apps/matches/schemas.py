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
    ScoringType,
    Team,
)

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

    The fields are ``create_match``'s, with the same defaults. The
    reason is stored in the match's audit log, which cannot be erased, so
    it should not name a player.
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
    NONE = "none"


class TeamConfirmationsOut(Schema):
    """Whether a player on each team has confirmed the match."""

    a: bool
    b: bool


class MatchOut(Schema):
    """A match, its teams, and its games, as the signed-in Account sees it.

    ``my_team`` is the viewer's own team, null when the viewer's Player did
    not play in the match. ``my_decision`` is the latest confirm or dispute
    this player recorded on the match, ``none`` when they recorded none or
    did not play. A repeated decision is not recorded again, so confirming
    again after a dispute that a moderator resolved still shows
    ``dispute``. An organizer's confirmation is not a player decision: a
    player who confirms as a member of the match's Event Organizer still
    shows ``none``. For a participant, ``confirmed_by_team`` says whether
    a player on each team recorded a confirmation (a dispute is not a
    confirmation); it is null for anyone else. A confirmation still shows
    in ``confirmed_by_team`` after the same player disputes, since a
    recorded decision is never withdrawn. It counts player confirmations
    only: a match confirmed by an organizer or a moderator, or a
    correction, can be confirmed with both false, so read ``status`` for
    whether the match is confirmed. ``created_at`` is when the match was
    submitted.
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
