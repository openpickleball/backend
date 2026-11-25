"""Matches: the games two teams played, who played them, and who sent them.

A Match is one entry in the match ledger (CONTEXT.md, Match and Ledger).
The ledger is append-only (CLAUDE.md, hard rule 3): a counted match is
never edited in place or deleted. A correction is a new record that
references the old one. The workflow services in ``apps.matches.workflow``
make every change, with their Audit Events.

The ORM guards the ledger (``MatchImmutable``): no Match, Match
Participant, Game, Match Confirmation, or Submission Batch can be deleted;
an existing Match can be saved only with ``update_fields=["status"]``; the
other four are inserted once and never saved again; and
``QuerySet.update``, ``delete``, ``bulk_update``, and an upsert through
``bulk_create`` raise on all five, through the default and the base
manager. Raw SQL and raw saves
(``loaddata``) are not guarded.

These models enforce only what a database constraint can: the allowed
values of each choice, one submitter, a ``played_at`` that agrees with its
local date, external IDs unique per Partner Application, at most
``MAX_GAMES`` games, no negative scores, and one Match that is not voided per
fingerprint. Score validation (win by 2, team size per format, games that
decide the match) is ``apps.matches.services.validate_match``, duplicate
detection is ``apps.matches.services.record_match``, and confirmation and
status changes are ``apps.matches.workflow``. Which matches count is
``MatchQuerySet.counted``. A possible duplicate is held for review as a
``MatchFlag``, so reviewing it (#40) writes a flag row, never the Match.

``ScoringType`` values equal ``open_engine.ScoringType``'s and
``GameTarget`` values equal the points in the engine's ``ALL_GAME_FORMATS``,
so the engine adapter (``apps.ratings.engine_adapter``) maps them
directly. ``MatchFormat`` holds only the two base values of
``open_engine.RatingFormat``: the engine adapter derives the doubles
categories (mixed, men's, women's) from the players. ``MatchStatus``, ``BestOf``, and ``Team``
have no engine enum. ``MatchType`` values equal ``open_engine.MatchType``'s.
Each Match stores its match type when it is recorded: the Event's kind,
``rec_verified`` for a Club with no Event, or ``self_reported`` for neither
(decision F1 in #1, ADR 0035). A check constraint ties it to the Event and
Club links, and the ledger guard keeps it from changing, so a later change
of the Event's kind moves only matches recorded after it. A correction
copies the type of the match it corrects.

A ``ResultsFile`` is an uploaded CSV results file and its report (#71). It
is not a ledger row and has no ledger guard: only
``apps.matches.results_files`` creates and changes it.
"""

import datetime
from collections.abc import Collection, Iterable
from datetime import UTC, timedelta
from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import (
    DateField,
    DateTimeField,
    DurationField,
    Exists,
    ExpressionWrapper,
    F,
    Func,
    OuterRef,
    Q,
    Value,
)
from django.db.models.functions import Cast
from django.utils import timezone

from apps.clubs.models import _CleanedTextModel, _LinkMessagesModel
from apps.events.models import CHOICE_MESSAGES
from apps.public_ids.codes import CodeType

# Shared limits, defined once. Score validation (services.py) imports them.
MAX_GAMES = 5
MIN_UTC_OFFSET_MINUTES = -720
MAX_UTC_OFFSET_MINUTES = 840

# Every character Python's str.isspace() is true for, so the database check
# agrees with the model's str.strip(). Postgres \s depends on the database
# locale and misses some of them. Listed one by one, since a bracket range
# may depend on the collation.
WHITESPACE_CODE_POINTS: tuple[int, ...] = (
    *range(0x0009, 0x000D + 1),
    *range(0x001C, 0x001F + 1),
    0x0020,
    0x0085,
    0x00A0,
    0x1680,
    *range(0x2000, 0x200A + 1),
    0x2028,
    0x2029,
    0x202F,
    0x205F,
    0x3000,
)
_WHITESPACE_CLASS = "[" + "".join(chr(point) for point in WHITESPACE_CODE_POINTS) + "]"

# Messages never include the submitted value (CLAUDE.md, API). Django's
# defaults for these keys do.
_NUMBER_MESSAGES = {"invalid": "Enter a whole number."}
_INTEGER_CHOICE_MESSAGES = {**CHOICE_MESSAGES, **_NUMBER_MESSAGES}
_DATE_MESSAGE = "Enter a valid date in YYYY-MM-DD format."
_DATE_MESSAGES = {"invalid": _DATE_MESSAGE, "invalid_date": _DATE_MESSAGE}
_DATETIME_MESSAGE = "Enter a valid date and time."
_DATETIME_MESSAGES = {
    "invalid": _DATETIME_MESSAGE,
    "invalid_date": _DATE_MESSAGE,
    "invalid_datetime": _DATETIME_MESSAGE,
}
# Django's default names the submitted row id.
_EVENT_MESSAGES = {"invalid": "Choose an existing event."}
_CLUB_MESSAGES = {"invalid": "Choose an existing club."}
_MATCH_MESSAGES = {"invalid": "Choose an existing match."}
_PLAYER_MESSAGES = {"invalid": "Choose an existing player."}
_USER_MESSAGES = {"invalid": "Choose an existing account."}
_APPLICATION_MESSAGES = {"invalid": "Choose an existing partner application."}
_BATCH_MESSAGES = {"invalid": "Choose an existing submission batch."}

_ONE_SUBMITTER = "A match has exactly one submitter: an account or an application."
_BATCH_ONE_SUBMITTER = (
    "A submission batch has exactly one submitter: an account or an application."
)
_EXTERNAL_ID_NEEDS_APPLICATION = (
    "Only a match submitted by a partner application can have an external ID."
)
_EXTERNAL_ID_TAKEN = "This application already submitted a match with this external ID."
_PLAYED_AT_NEEDS_OFFSET = (
    "Give the UTC offset where the match was played with the time played."
)
_OFFSET_NEEDS_PLAYED_AT = (
    "Give the time played with the UTC offset where the match was played."
)
_PLAYED_AT_NAIVE = "Give the time played with its time zone."
_PLAYED_AT_OUT_OF_RANGE = "Enter a time played within the supported dates."
_LOCAL_DATE = "The date played must be the local date of the time played."
_RATING_FORMAT_FITS = "The rating format must fit the format."
_MATCH_TYPE_FITS = "The match type must fit the match's event and club."

# played_at shifted to local time where the match was played, as a date. Postgres keeps a
# timestamptz in UTC and drops its offset, so the offset is stored apart.
# The explicit AT TIME ZONE makes the result independent of the session's
# time zone. (TruncDate's tzinfo cannot be written to a migration.)
_PLAYED_AT_UTC = Func(
    F("played_at"),
    template="(%(expressions)s AT TIME ZONE 'UTC')",
    output_field=DateTimeField(),
)
_LOCAL_PLAYED_ON = Cast(
    _PLAYED_AT_UTC
    + ExpressionWrapper(
        F("played_at_utc_offset") * Value(timedelta(minutes=1)),
        output_field=DurationField(),
    ),
    output_field=DateField(),
)


class MatchImmutable(RuntimeError):
    """Raised when code tries to change or delete a ledger row.

    The message is fixed text.
    """

    message: ClassVar[str] = (
        "The match ledger is append-only: a match, its players, its games, "
        "its confirmations, and submission batches cannot be changed or deleted."
    )

    def __init__(self) -> None:
        super().__init__(self.message)


class LedgerQuerySet[LedgerModelT: models.Model](models.QuerySet[LedgerModelT]):
    """Refuses bulk changes and deletes of ledger rows before any SQL runs.

    ``bulk_create`` still inserts, but an upsert (``update_conflicts``) is
    refused, since it would update an existing row.
    """

    def update(self, **kwargs: Any) -> NoReturn:
        raise MatchImmutable

    def delete(self) -> NoReturn:
        raise MatchImmutable

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise MatchImmutable

    def bulk_create(
        self,
        objs: Iterable[LedgerModelT],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[LedgerModelT]:
        if update_conflicts:
            raise MatchImmutable
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class _InsertOnlyModel(models.Model):
    """A ledger row that is inserted once and never saved again or deleted.

    Only a new instance with no primary key may be saved, as a plain
    insert. Raw writes (``loaddata``, raw SQL) skip this.
    """

    class Meta:
        abstract = True

    def save_base(
        self,
        raw: bool = False,
        force_insert: bool | tuple[models.base.ModelBase, ...] = False,
        force_update: bool = False,
        using: str | None = None,
        update_fields: Iterable[str] | None = None,
    ) -> None:
        if (
            force_update
            or update_fields is not None
            or not self._state.adding
            or self.pk is not None
        ):
            raise MatchImmutable
        super().save_base(
            raw=raw,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise MatchImmutable


class MatchQuerySet(LedgerQuerySet["Match"]):
    """Matches, with the ledger guard."""

    def counted(self) -> "MatchQuerySet":
        """The matches that count toward ratings.

        A match counts when it is confirmed and no open (unresolved)
        ``possible_duplicate`` flag links it, as ``match`` or as
        ``duplicate_of``, to a match that is not voided. This is ADR 0011's
        hold, refined so that a voided match (expired or corrected) stops
        holding the other one, and a flag a moderator resolved holds
        nothing (``apps.matches.workflow.resolve_flags``).
        """
        flags = MatchFlag.objects.filter(
            reason=FlagReason.POSSIBLE_DUPLICATE, resolved_at__isnull=True
        )
        held_as_match = flags.filter(match=OuterRef("pk")).exclude(
            duplicate_of__status=MatchStatus.VOIDED
        )
        held_as_duplicate = flags.filter(duplicate_of=OuterRef("pk")).exclude(
            match__status=MatchStatus.VOIDED
        )
        return self.filter(status=MatchStatus.CONFIRMED).exclude(
            Exists(held_as_match) | Exists(held_as_duplicate)
        )


class MatchParticipantQuerySet(LedgerQuerySet["MatchParticipant"]):
    """Match Participants, with the ledger guard."""


class GameQuerySet(LedgerQuerySet["Game"]):
    """Games, with the ledger guard."""


class MatchConfirmationQuerySet(LedgerQuerySet["MatchConfirmation"]):
    """Match Confirmations, with the ledger guard."""


class SubmissionBatchQuerySet(LedgerQuerySet["SubmissionBatch"]):
    """Submission Batches, with the ledger guard."""


class MatchFormat(models.TextChoices):
    """Singles or doubles.

    The values are the two base values of ``open_engine.RatingFormat``. The
    engine's doubles categories (mixed, men's, women's) are not stored here:
    the engine adapter (``apps.ratings.engine_adapter``) derives them from
    the players.
    """

    SINGLES = "singles", "Singles"
    DOUBLES = "doubles", "Doubles"


class RatingFormat(models.TextChoices):
    """The rating pool a Match counts in (ADR 0024).

    The values are ``open_engine.RatingFormat``'s. ``record_match`` stores
    one per Match from the players' gender divisions when the match is
    recorded (``apps.matches.services.derive_rating_format``), and it never
    changes afterward. A singles match is ``singles``; a doubles match is
    one of the four doubles values.
    """

    SINGLES = "singles", "Singles"
    DOUBLES = "doubles", "Doubles"
    MIXED_DOUBLES = "mixed_doubles", "Mixed doubles"
    DOUBLES_MEN = "doubles_men", "Men's doubles"
    DOUBLES_WOMEN = "doubles_women", "Women's doubles"


# The rating formats each format allows.
RATING_FORMATS_BY_FORMAT: dict[str, frozenset[str]] = {
    MatchFormat.SINGLES: frozenset({RatingFormat.SINGLES}),
    MatchFormat.DOUBLES: frozenset(
        {
            RatingFormat.DOUBLES,
            RatingFormat.MIXED_DOUBLES,
            RatingFormat.DOUBLES_MEN,
            RatingFormat.DOUBLES_WOMEN,
        }
    ),
}


class MatchType(models.TextChoices):
    """The match type a Match counts as (ADR 0024, ADR 0035).

    The values are ``open_engine.MatchType``'s. ``record_match`` stores one
    per Match when it is recorded (``apps.matches.services.draft_match``):
    the Event's kind, ``rec_verified`` for a Club with no Event, and
    ``self_reported`` for neither. It never changes afterward, so a later
    change of an Event's kind moves only matches recorded after it. A
    correction (``edit_match``) copies the type of the match it corrects.
    """

    TOURNAMENT = "tournament", "Tournament"
    LEAGUE = "league", "League"
    REC_VERIFIED = "rec_verified", "Verified rec play"
    SELF_REPORTED = "self_reported", "Self-reported"


# The match types a Match with an Event allows: the Event kinds.
EVENT_MATCH_TYPES: tuple[str, ...] = (
    MatchType.TOURNAMENT,
    MatchType.LEAGUE,
    MatchType.REC_VERIFIED,
)


def source_match_type(*, has_event: bool, has_club: bool) -> MatchType | None:
    """The one match type a Match without an Event allows, from its Club.

    None for a Match with an Event: its type is the Event's kind, one of
    ``EVENT_MATCH_TYPES``.
    """
    if has_event:
        return None
    return MatchType.REC_VERIFIED if has_club else MatchType.SELF_REPORTED


class MatchStatus(models.TextChoices):
    """Where a Match is in confirmation.

    Only ``record_match`` (the initial status) and the services in
    ``apps.matches.workflow`` set it.
    """

    PENDING = "pending", "Pending"
    CONFIRMED = "confirmed", "Confirmed"
    DISPUTED = "disputed", "Disputed"
    VOIDED = "voided", "Voided"


class ScoringType(models.TextChoices):
    """How points are scored. The values are ``open_engine.ScoringType``'s."""

    SIDE_OUT = "side_out", "Side-out"
    RALLY = "rally", "Rally"


class BestOf(models.IntegerChoices):
    """How many games a Match is played to (decision F15 in #1)."""

    ONE = 1, "Best of 1"
    THREE = 3, "Best of 3"
    FIVE = 5, "Best of 5"


class GameTarget(models.IntegerChoices):
    """The points a game is played to (decision F15 in #1).

    The values are the points in the engine's ``ALL_GAME_FORMATS``.
    """

    ELEVEN = 11, "11 points"
    FIFTEEN = 15, "15 points"
    TWENTY_ONE = 21, "21 points"


class Team(models.TextChoices):
    """One side of a Match."""

    A = "A", "Team A"
    B = "B", "Team B"


class SubmissionBatch(_LinkMessagesModel, _InsertOnlyModel):
    """The matches one submitter sent in one upload (#205, CONTEXT.md).

    A batch path (a CSV upload, #71, or a bulk API request, #72) creates
    one batch and passes it to ``record_match`` for each match, which sets
    ``Match.submission_batch``. A moderator can then void every match of
    the batch at once (``apps.matches.workflow.void_submission_batch``),
    found by this link, never by a time range.

    Exactly one of ``submitted_by_user`` and ``submitted_by_application``
    is set, and ``record_match`` refuses a batch whose submitter is not the
    match's. ``event`` and ``club`` are optional: when set, every match of
    the batch has that Event or Club. Like the ledger rows, a batch is
    inserted once and never changed or deleted (``MatchImmutable``). It has
    no OPEN Code: no API shows it.
    """

    submitted_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submission_batches",
        error_messages=_USER_MESSAGES,
    )
    submitted_by_application = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submission_batches",
        error_messages=_APPLICATION_MESSAGES,
    )
    event = models.ForeignKey(
        "events.Event",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submission_batches",
        error_messages=_EVENT_MESSAGES,
    )
    club = models.ForeignKey(
        "clubs.Club",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submission_batches",
        error_messages=_CLUB_MESSAGES,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SubmissionBatchQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "submission batches"
        # Django's internal queries use the base manager, so it must be the
        # guarded one too.
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    Q(submitted_by_user__isnull=False)
                    & Q(submitted_by_application__isnull=True)
                )
                | (
                    Q(submitted_by_user__isnull=True)
                    & Q(submitted_by_application__isnull=False)
                ),
                name="matches_submissionbatch_one_submitter",
                violation_error_message=_BATCH_ONE_SUBMITTER,
            ),
        ]

    def __str__(self) -> str:
        # Never the submitter: an Account's __str__ is its email.
        return f"Submission batch {self.pk}"


class Match(_LinkMessagesModel, _CleanedTextModel):
    """One match in the ledger (CONTEXT.md, Match).

    Exactly one of ``submitted_by_user`` and ``submitted_by_application`` is
    set. ``external_id`` is the submitting application's own id for the
    match, so only an application's match has one.

    ``played_at`` and ``played_at_utc_offset`` are both set or both empty.
    The offset is minutes east of UTC where the match was played, and
    ``played_at`` shifted by it falls on ``played_on``.

    ``location`` is the place's name, such as a court or park. It is never a
    street address or an exact location (ADR 0009).
    """

    public_code_type = CodeType.MATCH
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = (
        "external_id",
        "location",
        "event_label",
        "bracket_label",
    )

    format = models.CharField(
        max_length=10, choices=MatchFormat, error_messages=CHOICE_MESSAGES
    )
    # Set once at creation from the players' gender divisions (ADR 0024).
    # The default is the only value a singles match allows; a doubles match
    # left at it fails the check constraint below.
    rating_format = models.CharField(
        max_length=16,
        choices=RatingFormat,
        default=RatingFormat.SINGLES,
        error_messages=CHOICE_MESSAGES,
    )
    # Set once at creation from the Event's kind, or the Club (ADR 0024,
    # ADR 0035). A check constraint ties it to the Event and Club links.
    match_type = models.CharField(
        max_length=16,
        choices=MatchType,
        default=MatchType.SELF_REPORTED,
        error_messages=CHOICE_MESSAGES,
    )
    event = models.ForeignKey(
        "events.Event",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="matches",
        error_messages=_EVENT_MESSAGES,
    )
    club = models.ForeignKey(
        "clubs.Club",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="matches",
        error_messages=_CLUB_MESSAGES,
    )
    played_on = models.DateField(error_messages=_DATE_MESSAGES)
    played_at = models.DateTimeField(
        null=True, blank=True, error_messages=_DATETIME_MESSAGES
    )
    played_at_utc_offset = models.SmallIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(MIN_UTC_OFFSET_MINUTES),
            MaxValueValidator(MAX_UTC_OFFSET_MINUTES),
        ],
        error_messages=_NUMBER_MESSAGES,
        help_text="Minutes east of UTC where the match was played",
    )
    sequence = models.PositiveIntegerField(default=0, error_messages=_NUMBER_MESSAGES)
    status = models.CharField(
        max_length=10,
        choices=MatchStatus,
        default=MatchStatus.PENDING,
        error_messages=CHOICE_MESSAGES,
    )
    best_of = models.PositiveSmallIntegerField(
        choices=BestOf, error_messages=_INTEGER_CHOICE_MESSAGES
    )
    game_target = models.PositiveSmallIntegerField(
        choices=GameTarget,
        default=GameTarget.ELEVEN,
        error_messages=_INTEGER_CHOICE_MESSAGES,
    )
    scoring_type = models.CharField(
        max_length=10,
        choices=ScoringType,
        default=ScoringType.SIDE_OUT,
        error_messages=CHOICE_MESSAGES,
    )
    submitted_by_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_matches",
        error_messages=_USER_MESSAGES,
    )
    submitted_by_application = models.ForeignKey(
        settings.OAUTH2_PROVIDER_APPLICATION_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="submitted_matches",
        error_messages=_APPLICATION_MESSAGES,
    )
    external_id = models.CharField(max_length=128, blank=True, default="")
    location = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="The place's name, such as a court or park. Never a street address.",
    )
    event_label = models.CharField(max_length=200, blank=True, default="")
    bracket_label = models.CharField(max_length=200, blank=True, default="")
    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        editable=False,
        help_text=(
            "SHA-256 hex digest of the teams, date played, and game scores, "
            "set by record_match()"
        ),
    )
    corrects = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="corrected_by",
        error_messages=_MATCH_MESSAGES,
        help_text="The voided match this match corrects, set only by edit_match()",
    )
    submission_batch = models.ForeignKey(
        SubmissionBatch,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="matches",
        error_messages=_BATCH_MESSAGES,
        help_text="The upload this match came in, set only by record_match()",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = MatchQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "matches"
        # Django's internal queries use the base manager, so it must be the
        # guarded one too.
        base_manager_name = "objects"
        permissions = (("moderate_match", "Can void matches and resolve disputes"),)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    Q(submitted_by_user__isnull=False)
                    & Q(submitted_by_application__isnull=True)
                )
                | (
                    Q(submitted_by_user__isnull=True)
                    & Q(submitted_by_application__isnull=False)
                ),
                name="matches_match_one_submitter",
            ),
            models.CheckConstraint(
                condition=Q(format__in=MatchFormat.values),
                name="matches_match_format_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    format=MatchFormat.SINGLES, rating_format=RatingFormat.SINGLES
                )
                | Q(
                    format=MatchFormat.DOUBLES,
                    rating_format__in=sorted(
                        RATING_FORMATS_BY_FORMAT[MatchFormat.DOUBLES]
                    ),
                ),
                name="matches_match_rating_format_fits_format",
                violation_error_message=_RATING_FORMAT_FITS,
            ),
            models.CheckConstraint(
                condition=Q(event__isnull=False, match_type__in=EVENT_MATCH_TYPES)
                | Q(
                    event__isnull=True,
                    club__isnull=False,
                    match_type=MatchType.REC_VERIFIED,
                )
                | Q(
                    event__isnull=True,
                    club__isnull=True,
                    match_type=MatchType.SELF_REPORTED,
                ),
                name="matches_match_match_type_fits_source",
                violation_error_message=_MATCH_TYPE_FITS,
            ),
            models.CheckConstraint(
                condition=Q(status__in=MatchStatus.values),
                name="matches_match_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(scoring_type__in=ScoringType.values),
                name="matches_match_scoring_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(best_of__in=BestOf.values),
                name="matches_match_best_of_valid",
            ),
            models.CheckConstraint(
                condition=Q(game_target__in=GameTarget.values),
                name="matches_match_game_target_valid",
            ),
            models.CheckConstraint(
                condition=Q(played_at__isnull=True, played_at_utc_offset__isnull=True)
                | Q(played_at__isnull=False, played_at_utc_offset__isnull=False),
                name="matches_match_played_at_pair",
            ),
            models.CheckConstraint(
                condition=Q(
                    played_at_utc_offset__gte=MIN_UTC_OFFSET_MINUTES,
                    played_at_utc_offset__lte=MAX_UTC_OFFSET_MINUTES,
                ),
                name="matches_match_played_at_utc_offset_range",
            ),
            models.CheckConstraint(
                condition=Q(played_on=_LOCAL_PLAYED_ON),
                name="matches_match_played_at_local_date",
            ),
            models.CheckConstraint(
                condition=Q(external_id="") | Q(submitted_by_application__isnull=False),
                name="matches_match_external_id_needs_application",
            ),
            models.CheckConstraint(
                # Raw writes skip the model's stripping.
                condition=~Q(
                    external_id__regex=f"^{_WHITESPACE_CLASS}|{_WHITESPACE_CLASS}$"
                ),
                name="matches_match_external_id_trimmed",
            ),
            # A corrected match keeps its application's external id, so only
            # matches that are not voided must differ. record_match() still
            # refuses an external id used by a match in any status.
            models.UniqueConstraint(
                fields=("submitted_by_application", "external_id"),
                condition=~Q(external_id="") & ~Q(status=MatchStatus.VOIDED),
                name="matches_match_external_id_unique_per_application",
                violation_error_message=_EXTERNAL_ID_TAKEN,
            ),
            models.UniqueConstraint(
                fields=("fingerprint",),
                condition=~Q(fingerprint="") & ~Q(status=MatchStatus.VOIDED),
                name="matches_match_fingerprint_unique_unless_voided",
                violation_error_message="This match was already submitted.",
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never a name, for one rule with Club and Event.
        return self.public_code

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Insert a new Match, or save only the status of an existing one.

        An existing Match may be saved only with ``update_fields`` naming
        ``status`` alone, which the workflow services in
        ``apps.matches.workflow`` do under a row lock. Any other save of an
        existing Match raises ``MatchImmutable``. Raw writes (``loaddata``,
        raw SQL) skip this.
        """
        if not self._state.adding:
            update_fields = kwargs.get("update_fields")
            if update_fields is None or set(update_fields) != {"status"}:
                raise MatchImmutable
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise MatchImmutable

    def is_counted(self) -> bool:
        """True when this match counts, read from the database.

        See ``MatchQuerySet.counted``. An unsaved match never counts.
        """
        if self.pk is None:
            return False
        return Match.objects.counted().filter(pk=self.pk).exists()

    def _match_type_fits(self) -> bool:
        """True when the match type fits the Event and Club links."""
        expected = source_match_type(
            has_event=self.event_id is not None, has_club=self.club_id is not None
        )
        if expected is None:
            return self.match_type in EVENT_MATCH_TYPES
        return self.match_type == expected

    def clean(self) -> None:
        # Django runs clean() even when a field failed, so a value may still
        # be the raw input here: check its type before using it.
        errors: dict[str, str] = {}
        has_user = self.submitted_by_user_id is not None
        has_application = self.submitted_by_application_id is not None
        if has_user == has_application:
            errors["submitted_by_user"] = _ONE_SUBMITTER
            errors["submitted_by_application"] = _ONE_SUBMITTER
        if self.external_id and not has_application:
            errors["external_id"] = _EXTERNAL_ID_NEEDS_APPLICATION
        allowed = (
            RATING_FORMATS_BY_FORMAT.get(self.format)
            if isinstance(self.format, str)
            else None
        )
        if (
            allowed is not None
            and isinstance(self.rating_format, str)
            and self.rating_format in RatingFormat.values
            and self.rating_format not in allowed
        ):
            errors["rating_format"] = _RATING_FORMAT_FITS
        if isinstance(self.match_type, str) and not self._match_type_fits():
            errors["match_type"] = _MATCH_TYPE_FITS

        played_at, offset = self.played_at, self.played_at_utc_offset
        if played_at is not None and offset is None:
            errors["played_at_utc_offset"] = _PLAYED_AT_NEEDS_OFFSET
        elif played_at is None and offset is not None:
            errors["played_at"] = _OFFSET_NEEDS_PLAYED_AT
        elif isinstance(played_at, datetime.datetime) and played_at.utcoffset() is None:
            errors["played_at"] = _PLAYED_AT_NAIVE
        elif (
            isinstance(played_at, datetime.datetime)
            and isinstance(offset, int)
            and MIN_UTC_OFFSET_MINUTES <= offset <= MAX_UTC_OFFSET_MINUTES
            and isinstance(self.played_on, datetime.date)
            and not isinstance(self.played_on, datetime.datetime)
        ):
            try:
                local = played_at.astimezone(UTC) + timedelta(minutes=offset)
            except OverflowError:
                errors["played_at"] = _PLAYED_AT_OUT_OF_RANGE
            else:
                if local.date() != self.played_on:
                    errors["played_on"] = _LOCAL_DATE
        if errors:
            raise ValidationError(errors)


class MatchParticipant(_LinkMessagesModel, _InsertOnlyModel):
    """A Player on one team of a Match. A Player plays once per Match.

    Inserted once with its Match and never saved again or deleted.
    """

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="participants",
        error_messages=_MATCH_MESSAGES,
    )
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        related_name="match_participations",
        error_messages=_PLAYER_MESSAGES,
    )
    team = models.CharField(max_length=1, choices=Team, error_messages=CHOICE_MESSAGES)

    objects = MatchParticipantQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(team__in=Team.values),
                name="matches_matchparticipant_team_valid",
            ),
            models.UniqueConstraint(
                fields=("match", "player"),
                name="matches_matchparticipant_player_once_per_match",
                violation_error_message="This player is already in this match.",
            ),
        ]

    def __str__(self) -> str:
        # The match code and team, never the Player's name.
        return f"{self.match.public_code} team {self.team}"


class Game(_LinkMessagesModel, _InsertOnlyModel):
    """One game of a Match, numbered from 1. Scores are never negative.

    Inserted once with its Match and never saved again or deleted.
    """

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="games",
        error_messages=_MATCH_MESSAGES,
    )
    game_number = models.PositiveSmallIntegerField(error_messages=_NUMBER_MESSAGES)
    team_a_score = models.PositiveSmallIntegerField(error_messages=_NUMBER_MESSAGES)
    team_b_score = models.PositiveSmallIntegerField(error_messages=_NUMBER_MESSAGES)

    objects = GameQuerySet.as_manager()

    class Meta:
        ordering = ("match", "game_number")
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(game_number__gte=1, game_number__lte=MAX_GAMES),
                name="matches_game_number_range",
            ),
            models.UniqueConstraint(
                fields=("match", "game_number"),
                name="matches_game_number_once_per_match",
                violation_error_message="This match already has a game with this number.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.match.public_code} game {self.game_number}"


class ConfirmationDecision(models.TextChoices):
    """What a participant said about a Match."""

    CONFIRM = "confirm", "Confirm"
    DISPUTE = "dispute", "Dispute"


class MatchConfirmation(_LinkMessagesModel, _InsertOnlyModel):
    """One participant's confirmation or dispute of a Match (#26).

    See CONTEXT.md, Confirmation. A Player confirms a Match at most once and
    disputes it at most once. Only the workflow services in
    ``apps.matches.workflow`` write these rows. Like the other ledger rows,
    a row is inserted once and never changed or deleted.
    """

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="confirmations",
        error_messages=_MATCH_MESSAGES,
    )
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        related_name="match_confirmations",
        error_messages=_PLAYER_MESSAGES,
    )
    decision = models.CharField(
        max_length=10, choices=ConfirmationDecision, error_messages=CHOICE_MESSAGES
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = MatchConfirmationQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(decision__in=ConfirmationDecision.values),
                name="matches_matchconfirmation_decision_valid",
            ),
            models.UniqueConstraint(
                fields=("match", "player", "decision"),
                name="matches_matchconfirmation_once_per_decision",
                violation_error_message="This player already gave this decision.",
            ),
        ]

    def __str__(self) -> str:
        # The match code and decision, never the Player's name.
        return f"{self.match.public_code} {self.decision}"


class FlagReason(models.TextChoices):
    """Why a Match was flagged for review."""

    POSSIBLE_DUPLICATE = "possible_duplicate", "Possible duplicate"


class FlagResolution(models.TextChoices):
    """How a moderator closed a flag (#40).

    ``approved``: the flagged result stands. ``dismissed``: the flag was
    not a real concern. Either way the flag stops holding its matches.
    """

    APPROVED = "approved", "Approved"
    DISMISSED = "dismissed", "Dismissed"


class MatchFlag(_LinkMessagesModel):
    """A reason to hold a Match for moderator review (#40).

    See CONTEXT.md, Flagged Match. ``record_match()`` creates a
    ``possible_duplicate`` flag for each earlier Match that is not voided,
    with the same teams and date but other scores, with ``duplicate_of``
    set to it. A flag is a row of its own, so reviewing one never edits the
    Match in the ledger.

    An open ``possible_duplicate`` flag holds both its ``match`` and its
    ``duplicate_of`` from counting until a moderator resolves it, whatever
    the new Match's status or submitter (ADR 0011). The one refinement
    (``MatchQuerySet.counted``): a voided match, expired or corrected, no
    longer holds the other one. The engine adapter counts only
    ``counted()`` matches (``counted_engine_matches``).

    The hold period (ADR 0034): a flag still open after
    ``MATCH_DUPLICATE_HOLD_DAYS`` whose earlier match is confirmed makes
    the daily ``expire_disputes_and_holds`` void its newer match. The
    earlier one is ``duplicate_of``, except that a correction keeps its
    original's place while its chain keeps the same teams and date: each
    side ranks by the first match of its own same-teams-and-date chain
    (through ``corrects``), and the one recorded first is the earlier one.
    That ends the hold on the earlier one, which counts again unless
    another open flag still holds it. A hold against a pending or disputed
    earlier match never ends this way. The flag itself stays open.

    A moderator resolves a flag through
    ``apps.matches.workflow.resolve_flags``, which sets ``resolution`` and
    ``resolved_at`` together (a check constraint). Both are empty while the
    flag is open. A flag row is never deleted. The resolver and reason are
    in its Audit Event, not on the flag.
    """

    match = models.ForeignKey(
        Match,
        on_delete=models.PROTECT,
        related_name="flags",
        error_messages=_MATCH_MESSAGES,
    )
    reason = models.CharField(
        max_length=30, choices=FlagReason, error_messages=CHOICE_MESSAGES
    )
    duplicate_of = models.ForeignKey(
        Match,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="duplicate_flags",
        error_messages=_MATCH_MESSAGES,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    resolution = models.CharField(
        max_length=20,
        choices=FlagResolution,
        blank=True,
        error_messages=CHOICE_MESSAGES,
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(reason__in=FlagReason.values),
                name="matches_matchflag_reason_valid",
            ),
            models.CheckConstraint(
                condition=Q(resolution="", resolved_at__isnull=True)
                | (
                    Q(resolution__in=FlagResolution.values)
                    & Q(resolved_at__isnull=False)
                ),
                name="matches_matchflag_resolution_set_together",
                violation_error_message=(
                    "A resolved flag has both a resolution and a time."
                ),
            ),
            models.CheckConstraint(
                condition=Q(
                    reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of__isnull=False
                )
                | (
                    ~Q(reason=FlagReason.POSSIBLE_DUPLICATE)
                    & Q(duplicate_of__isnull=True)
                ),
                name="matches_matchflag_duplicate_of_matches_reason",
                violation_error_message=(
                    "A possible duplicate flag names the match it may duplicate."
                ),
            ),
            models.CheckConstraint(
                condition=~Q(duplicate_of=F("match")),
                name="matches_matchflag_not_self",
                violation_error_message="A match cannot duplicate itself.",
            ),
            models.UniqueConstraint(
                fields=("match", "duplicate_of"),
                name="matches_matchflag_once_per_pair",
                violation_error_message=(
                    "This match is already flagged as a duplicate of that match."
                ),
            ),
        ]

    def __str__(self) -> str:
        # The match code and reason, never a name.
        return f"{self.match.public_code} flag {self.reason}"


# A Results File's location is stored on each of its matches.
MATCH_LOCATION_MAX: int = Match._meta.get_field("location").max_length or 0


class ResultsFileStatus(models.TextChoices):
    """Where a Results File is in its processing (#71)."""

    PENDING = "pending", "Pending"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


_ERROR_CODE_NEEDS_FAILED = "Only a failed results file has an error code."


class ResultsFile(_LinkMessagesModel, _CleanedTextModel):
    """A CSV results file an organizer sent, and its report (#71, CONTEXT.md).

    Created by ``apps.matches.results_files.create_results_file`` with its
    Submission Batch (the uploader and the Event), and changed only by that
    module's processing and sweep. It is not a ledger row: its status,
    report, and content change once, when it is processed.

    ``content`` holds the raw file, which has player names and IDs, only
    until processing ends (done or failed); then it is emptied. ``report``
    holds one result per match row: row number, sequence, result, the
    Match's OPEN Code, and issues and warnings as columns, code, and fixed
    message. It never holds a name or a submitted cell value.
    ``error_code`` is set only for a file that failed as a whole.
    """

    public_code_type = CodeType.RESULTS_FILE
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("location",)

    submission_batch = models.OneToOneField(
        SubmissionBatch,
        on_delete=models.PROTECT,
        related_name="results_file",
        error_messages=_BATCH_MESSAGES,
    )
    game_target = models.PositiveSmallIntegerField(
        choices=GameTarget,
        default=GameTarget.ELEVEN,
        error_messages=_INTEGER_CHOICE_MESSAGES,
    )
    location = models.CharField(
        max_length=MATCH_LOCATION_MAX,
        blank=True,
        default="",
        help_text="The place's name, such as a court or park. Never a street address.",
    )
    status = models.CharField(
        max_length=10,
        choices=ResultsFileStatus,
        default=ResultsFileStatus.PENDING,
        error_messages=CHOICE_MESSAGES,
    )
    error_code = models.CharField(max_length=32, blank=True, default="")
    content = models.BinaryField(blank=True, default=b"", editable=False)
    report = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # When processing was last queued: the sweep queues a file again only
    # when this is old enough (``apps.matches.results_files``).
    queued_at = models.DateTimeField(default=timezone.now, editable=False)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(status__in=ResultsFileStatus.values),
                name="matches_resultsfile_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(game_target__in=GameTarget.values),
                name="matches_resultsfile_game_target_valid",
            ),
            models.CheckConstraint(
                condition=Q(status=ResultsFileStatus.FAILED) | Q(error_code=""),
                name="matches_resultsfile_error_code_needs_failed",
                violation_error_message=_ERROR_CODE_NEEDS_FAILED,
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code only, never the uploader or the file.
        return self.public_code
