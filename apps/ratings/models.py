"""Ratings: each player's current rating per format, its history, and replays.

``RatingState`` is a Player's current Rating in one rating format, one row
per Player and format. ``RatingHistory`` is one row per change to it, with
what caused the change: a live update from one Match, or a replay of the
Ledger (``RatingReplay``), or its removal by a replay that no longer
produces it. ``RatingReplay`` holds one replay's provenance (hard rule 4).
``RatingReplayRequest`` asks for a replay (#47): a change to what counts,
or a match a live update could not take, stores one, and the replay
deletes the requests it took. ADR 0025 records these decisions.

The values are what the engine returned (``engine_adapter.RatingSummary``,
hard rule 2): the backend stores them and never computes or rounds a
rating. ``reliability`` is OPEN-earned and ``imported_reliability`` is the
carried external reliability, kept apart (hard rule 5). Every stored
rating is a demonstration rating while the engine is a demonstration
package.

Write these rows only through ``apps.ratings.services``
(``apply_rating()``, ``record_replay()``, and the replay's removal path).
The ORM guards them:
``RatingHistory`` and ``RatingReplay`` rows are inserted once and never
saved again or deleted (``RatingHistoryImmutable``), and a ``RatingState``
is never bulk inserted or bulk changed, and deleted only by the replay's
private removal path (``services._remove_rating()``): its ``delete()``
and every queryset delete refuse (``RatingStateWriteRefused``).
``QuerySet.update``, ``delete``,
``bulk_update``, and an upsert raise on all three, through the default and
the base manager. ``bulk_create`` still inserts history and replay rows,
skipping the service's checks and Audit Event. Raw SQL and raw saves
(``loaddata``) are not guarded.
"""

import math
from collections.abc import Callable, Collection, Iterable
from typing import Any, ClassVar, NoReturn

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.clubs.models import _LinkMessagesModel
from apps.events.models import CHOICE_MESSAGES
from apps.matches.models import RatingFormat

# Messages never include the submitted value (CLAUDE.md, API). Django's
# defaults for these keys do.
_FLOAT_MESSAGES = {"invalid": "Enter a number."}
_NUMBER_MESSAGES = {"invalid": "Enter a whole number."}
_BOOLEAN_MESSAGES = {
    "invalid": "Enter true or false.",
    "invalid_nullable": "Enter true, false, or nothing.",
}
_DATE_MESSAGE = "Enter a valid date in YYYY-MM-DD format."
_DATE_MESSAGES = {"invalid": _DATE_MESSAGE, "invalid_date": _DATE_MESSAGE}
_PLAYER_MESSAGES = {"invalid": "Choose an existing player."}
_MATCH_MESSAGES = {"invalid": "Choose an existing match."}
_REPLAY_MESSAGES = {"invalid": "Choose an existing replay."}

_RELIABILITY_RANGE = "Reliability must be from 0 to 100."
_RD_NOT_NEGATIVE = "RD cannot be negative."
_NOT_FINITE = "Enter a finite number."
_PARAMS_VERSION_BLANK = (
    "The parameter set version is either empty or names a parameter set."
)
_ONE_STATE = "This player already has a rating in this format."
_KIND_LINKS = (
    "A live update names its match and no replay; a replay row names its "
    "replay; a removed row names its replay and no match."
)
_DELTA_LIVE_ONLY = "Only a live update has a delta."
_COUNTED_WITHIN_CONSIDERED = "A replay cannot count more matches than it considered."
_NET_RATING_CREATED = "Give a finite number for each rating format."
_ONE_LIVE_UPDATE = "This match already updated this player's rating in this format."

# Postgres orders NaN above every number, so a finite check needs both
# bounds: NaN fails "< Infinity".
_INFINITY = float("inf")

VERSION_MAX_LENGTH = 64
FINGERPRINT_MAX_LENGTH = 128


def _finite(value: float) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(_NOT_FINITE, code="not_finite")


def _finite_condition(field: str) -> Q:
    return Q(**{f"{field}__lt": _INFINITY, f"{field}__gt": -_INFINITY})


def _valid_net_rating_created(value: object) -> None:
    """A mapping of rating format values to finite numbers, never a bool."""
    if not isinstance(value, dict) or not all(
        key in RatingFormat.values
        and type(number) in (int, float)
        and math.isfinite(number)
        for key, number in value.items()
    ):
        raise ValidationError(_NET_RATING_CREATED, code="invalid")


_RELIABILITY_VALIDATORS: list[Callable[[Any], None]] = [
    _finite,
    MinValueValidator(0, message=_RELIABILITY_RANGE),
    MaxValueValidator(100, message=_RELIABILITY_RANGE),
]


class RatingChangeKind(models.TextChoices):
    """What caused a change to a player's rating."""

    LIVE_UPDATE = "live_update", "Live update"
    REPLAY = "replay", "Replay"
    REMOVED = "removed", "Removed by a replay"


class RatingHistoryImmutable(RuntimeError):
    """Raised when code tries to change or delete a history or replay row.

    The message is fixed text.
    """

    message: ClassVar[str] = (
        "Rating history and replays are append-only: they cannot be changed or deleted."
    )

    def __init__(self) -> None:
        super().__init__(self.message)


class RatingStateWriteRefused(RuntimeError):
    """Raised when code tries to delete, bulk insert, or bulk change a rating state.

    The message is fixed text.
    """

    message: ClassVar[str] = (
        "A rating state changes only through apply_rating(): it cannot be "
        "bulk inserted, bulk changed, or deleted."
    )

    def __init__(self) -> None:
        super().__init__(self.message)


class _GuardedQuerySet[ModelT: models.Model](models.QuerySet[ModelT]):
    """Refuses bulk changes and deletes before any SQL runs.

    ``bulk_create`` still inserts, but an upsert (``update_conflicts``) is
    refused, since it would update an existing row.
    """

    refused: ClassVar[type[RuntimeError]]

    def _refuse(self) -> NoReturn:
        raise self.refused()

    def update(self, **kwargs: Any) -> NoReturn:
        self._refuse()

    def delete(self) -> NoReturn:
        self._refuse()

    def bulk_update(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse()

    def bulk_create(
        self,
        objs: Iterable[ModelT],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[ModelT]:
        if update_conflicts:
            self._refuse()
        return super().bulk_create(
            objs,
            batch_size=batch_size,
            ignore_conflicts=ignore_conflicts,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )


class RatingStateQuerySet(_GuardedQuerySet["RatingState"]):
    """Rating states, never bulk inserted, bulk changed, or deleted.

    A bulk insert would skip ``apply_rating()``'s history row and Audit
    Event.
    """

    refused = RatingStateWriteRefused

    def bulk_create(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse()


class RatingHistoryQuerySet(_GuardedQuerySet["RatingHistory"]):
    """Rating history rows, append-only."""

    refused = RatingHistoryImmutable


class RatingReplayQuerySet(_GuardedQuerySet["RatingReplay"]):
    """Replays, append-only."""

    refused = RatingHistoryImmutable


class _InsertOnlyModel(models.Model):
    """A row that is inserted once and never saved again or deleted.

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
            raise RatingHistoryImmutable
        super().save_base(
            raw=raw,
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise RatingHistoryImmutable


def _not_blank(field: str, prefix: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=~Q(**{field: ""}), name=f"{prefix}_{field}_not_blank"
    )


def _params_version_not_blank(prefix: str) -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=Q(params_version__isnull=True) | ~Q(params_version=""),
        name=f"{prefix}_params_version_not_blank",
        violation_error_message=_PARAMS_VERSION_BLANK,
    )


def _value_constraints(prefix: str) -> list[models.BaseConstraint]:
    """The checks ``RatingState`` and ``RatingHistory`` share."""
    return [
        models.CheckConstraint(
            condition=Q(rating_format__in=RatingFormat.values),
            name=f"{prefix}_rating_format_valid",
        ),
        models.CheckConstraint(
            condition=Q(reliability__gte=0, reliability__lte=100),
            name=f"{prefix}_reliability_range",
            violation_error_message=_RELIABILITY_RANGE,
        ),
        models.CheckConstraint(
            condition=Q(imported_reliability__isnull=True)
            | Q(imported_reliability__gte=0, imported_reliability__lte=100),
            name=f"{prefix}_imported_reliability_range",
            violation_error_message=_RELIABILITY_RANGE,
        ),
        models.CheckConstraint(
            condition=Q(rd__gte=0),
            name=f"{prefix}_rd_not_negative",
            violation_error_message=_RD_NOT_NEGATIVE,
        ),
        models.CheckConstraint(
            condition=_finite_condition("rating"),
            name=f"{prefix}_rating_finite",
            violation_error_message=_NOT_FINITE,
        ),
        models.CheckConstraint(
            condition=_finite_condition("rd"),
            name=f"{prefix}_rd_finite",
            violation_error_message=_NOT_FINITE,
        ),
        *(
            _not_blank(field, prefix)
            for field in (
                "config_version",
                "parameter_fingerprint",
                "engine_version",
                "spec_version",
            )
        ),
        _params_version_not_blank(prefix),
    ]


class _RatingValues(_LinkMessagesModel):
    """The values of a player's rating in one format, as the engine gave them.

    The fields are ``engine_adapter.RatingSummary``'s, plus the engine and
    spec versions that produced them (hard rule 4). Each subclass adds its
    own ``player`` link.
    """

    rating_format = models.CharField(
        max_length=16, choices=RatingFormat, error_messages=CHOICE_MESSAGES
    )
    rating = models.FloatField(validators=[_finite], error_messages=_FLOAT_MESSAGES)
    rd = models.FloatField(
        validators=[_finite, MinValueValidator(0, message=_RD_NOT_NEGATIVE)],
        error_messages=_FLOAT_MESSAGES,
        help_text="Rating deviation, the engine's uncertainty",
    )
    reliability = models.FloatField(
        validators=_RELIABILITY_VALIDATORS,
        error_messages=_FLOAT_MESSAGES,
        help_text="OPEN-earned reliability, 0 to 100",
    )
    imported_reliability = models.FloatField(
        null=True,
        blank=True,
        validators=_RELIABILITY_VALIDATORS,
        error_messages=_FLOAT_MESSAGES,
        help_text="Carried external reliability, 0 to 100, shown apart",
    )
    established = models.BooleanField(error_messages=_BOOLEAN_MESSAGES)
    matches_played = models.PositiveIntegerField(error_messages=_NUMBER_MESSAGES)
    last_played = models.DateField(null=True, blank=True, error_messages=_DATE_MESSAGES)
    calculated_as_of = models.DateField(
        null=True,
        blank=True,
        error_messages=_DATE_MESSAGES,
        help_text="The date of the last replay; empty when never replayed",
    )
    live_updates_since_recalculation = models.PositiveIntegerField(
        error_messages=_NUMBER_MESSAGES
    )
    config_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    # None, not "", when the config is not a published set, as the engine
    # reports it. A check constraint refuses "".
    params_version = models.CharField(  # noqa: DJ001
        max_length=VERSION_MAX_LENGTH,
        null=True,
        blank=True,
        help_text="The published parameter set label; empty when it is not one",
    )
    parameter_fingerprint = models.CharField(max_length=FINGERPRINT_MAX_LENGTH)
    engine_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    spec_version = models.CharField(max_length=VERSION_MAX_LENGTH)

    class Meta:
        abstract = True

    @property
    def provisional(self) -> bool:
        """True when the rating holds live updates not yet replayed (hard rule 3).

        A rating never replayed is provisional too.
        """
        return (
            self.calculated_as_of is None or self.live_updates_since_recalculation > 0
        )


class RatingReplay(_InsertOnlyModel):
    """One replay of the Ledger and its provenance (hard rule 4).

    The fields are ``engine_adapter.ReplayProvenance``'s, plus the rating
    spec version (``settings.OPEN_RATING_SPEC_VERSION``). Holds no personal
    data. Append-only.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    as_of = models.DateField(error_messages=_DATE_MESSAGES)
    config_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    config_fingerprint = models.CharField(max_length=FINGERPRINT_MAX_LENGTH)
    params_version = models.CharField(  # noqa: DJ001 (None as the engine reports it)
        max_length=VERSION_MAX_LENGTH, null=True, blank=True
    )
    parameter_fingerprint = models.CharField(max_length=FINGERPRINT_MAX_LENGTH)
    matches_considered = models.PositiveIntegerField(error_messages=_NUMBER_MESSAGES)
    matches_counted = models.PositiveIntegerField(error_messages=_NUMBER_MESSAGES)
    net_rating_created = models.JSONField(
        blank=True,
        validators=[_valid_net_rating_created],
        help_text="The rating each format's counted updates added, by format",
    )
    engine_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    spec_version = models.CharField(max_length=VERSION_MAX_LENGTH)

    objects = RatingReplayQuerySet.as_manager()

    class Meta:
        ordering = ("created_at", "id")
        # Django's internal queries use the base manager, so it must be the
        # guarded one too.
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *(
                _not_blank(field, "ratings_ratingreplay")
                for field in (
                    "config_version",
                    "config_fingerprint",
                    "parameter_fingerprint",
                    "engine_version",
                    "spec_version",
                )
            ),
            _params_version_not_blank("ratings_ratingreplay"),
            models.CheckConstraint(
                condition=Q(matches_counted__lte=models.F("matches_considered")),
                name="ratings_ratingreplay_counted_within_considered",
                violation_error_message=_COUNTED_WITHIN_CONSIDERED,
            ),
        ]

    def __str__(self) -> str:
        return f"Replay as of {self.as_of.isoformat()}"

    def clean(self) -> None:
        # The field's validator skips empty values ([], "", ()), so the
        # mapping check runs here too. {} is allowed.
        if not isinstance(self.net_rating_created, dict):
            raise ValidationError({"net_rating_created": _NET_RATING_CREATED})


class RatingState(_RatingValues):
    """A Player's current Rating in one rating format.

    One row per Player and format. Change it only through
    ``apps.ratings.services.apply_rating()``, which records its history row
    and Audit Event. It is never bulk changed, and deleted only by the
    replay's private removal path (``services._remove_rating()``), when a
    replay no longer produces it; ``delete()`` and ``QuerySet.delete()``
    refuse.
    """

    player = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        related_name="rating_states",
        error_messages=_PLAYER_MESSAGES,
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = RatingStateQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("player", "rating_format"),
                name="ratings_ratingstate_one_per_player_and_format",
                violation_error_message=_ONE_STATE,
            ),
            *_value_constraints("ratings_ratingstate"),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never a name.
        return f"{self.player.public_code} {self.rating_format}"

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise RatingStateWriteRefused


class RatingHistory(_RatingValues, _InsertOnlyModel):
    """One change to a Player's Rating in one format, and what caused it.

    A live update names its ``match`` and the engine's change for the
    player (``delta``). A replay row names its ``replay``. The values are
    the rating after the change. A removed row names the replay that no
    longer produced the rating, and holds the values removed. Append-only.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    player = models.ForeignKey(
        "players.Player",
        on_delete=models.PROTECT,
        related_name="rating_history",
        error_messages=_PLAYER_MESSAGES,
    )
    kind = models.CharField(
        max_length=16, choices=RatingChangeKind, error_messages=CHOICE_MESSAGES
    )
    match = models.ForeignKey(
        "matches.Match",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="rating_history",
        error_messages=_MATCH_MESSAGES,
    )
    replay = models.ForeignKey(
        RatingReplay,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="history",
        error_messages=_REPLAY_MESSAGES,
    )
    delta = models.FloatField(
        null=True,
        blank=True,
        validators=[_finite],
        error_messages=_FLOAT_MESSAGES,
        help_text="The engine's change for this player from the match",
    )

    objects = RatingHistoryQuerySet.as_manager()

    class Meta:
        verbose_name_plural = "rating history"
        ordering = ("created_at", "id")
        base_manager_name = "objects"
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=["player", "rating_format", "created_at"],
                name="ratings_history_player_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *_value_constraints("ratings_ratinghistory"),
            models.CheckConstraint(
                condition=Q(kind__in=RatingChangeKind.values),
                name="ratings_ratinghistory_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    kind=RatingChangeKind.LIVE_UPDATE,
                    match__isnull=False,
                    replay__isnull=True,
                )
                | Q(kind=RatingChangeKind.REPLAY, replay__isnull=False)
                | Q(
                    kind=RatingChangeKind.REMOVED,
                    replay__isnull=False,
                    match__isnull=True,
                ),
                name="ratings_ratinghistory_kind_links",
                violation_error_message=_KIND_LINKS,
            ),
            models.CheckConstraint(
                condition=Q(delta__isnull=True) | Q(kind=RatingChangeKind.LIVE_UPDATE),
                name="ratings_ratinghistory_delta_live_only",
                violation_error_message=_DELTA_LIVE_ONLY,
            ),
            models.CheckConstraint(
                condition=Q(delta__isnull=True) | _finite_condition("delta"),
                name="ratings_ratinghistory_delta_finite",
                violation_error_message=_NOT_FINITE,
            ),
            # A redelivered live update must not apply twice.
            models.UniqueConstraint(
                fields=("player", "rating_format", "match"),
                condition=Q(kind=RatingChangeKind.LIVE_UPDATE),
                name="ratings_ratinghistory_one_live_update_per_match",
                violation_error_message=_ONE_LIVE_UPDATE,
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never a name.
        return f"{self.player.public_code} {self.rating_format} {self.kind}"


class EngineSnapshotWriteRefused(RuntimeError):
    """Raised when code tries to delete, bulk insert, or bulk change a snapshot.

    The message is fixed text.
    """

    message: ClassVar[str] = (
        "An engine snapshot changes only through the live update and the replay: it "
        "cannot be bulk inserted, bulk changed, or deleted."
    )

    def __init__(self) -> None:
        super().__init__(self.message)


class EngineSnapshotQuerySet(_GuardedQuerySet["EngineSnapshot"]):
    """Engine snapshots, never bulk inserted, bulk changed, or deleted."""

    refused = EngineSnapshotWriteRefused

    def bulk_create(self, *args: Any, **kwargs: Any) -> NoReturn:
        self._refuse()


_SNAPSHOT_NOT_MAPPING = "The snapshot must be a mapping."


class EngineSnapshot(models.Model):
    """The engine's whole state that live updates build on (#46, ADR 0027).

    One row per parameter fingerprint, the published parameter set's
    values. ``snapshot`` is ``RatingEngine.to_snapshot()`` output, empty
    until the first live update or replay. It holds Player OPEN Codes and
    their rating states, never a name. Its writers are all in
    ``apps.ratings.services``: the live update
    (``update_ratings_for_match()``), the replay (``replay_ratings()``),
    and the recompute (``recompute_ratings()``, #48). Each locks the row
    first, so they serialize. It is never deleted or
    bulk changed.
    """

    parameter_fingerprint = models.CharField(
        max_length=FINGERPRINT_MAX_LENGTH, unique=True
    )
    config_fingerprint = models.CharField(max_length=FINGERPRINT_MAX_LENGTH)
    config_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    params_version = models.CharField(  # noqa: DJ001 (None as the engine reports it)
        max_length=VERSION_MAX_LENGTH, null=True, blank=True
    )
    engine_version = models.CharField(max_length=VERSION_MAX_LENGTH)
    snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text="RatingEngine.to_snapshot() output; empty before the first update",
    )
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    objects = EngineSnapshotQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            *(
                _not_blank(field, "ratings_enginesnapshot")
                for field in (
                    "parameter_fingerprint",
                    "config_fingerprint",
                    "config_version",
                    "engine_version",
                )
            ),
            _params_version_not_blank("ratings_enginesnapshot"),
        ]

    def __str__(self) -> str:
        return str(self.parameter_fingerprint)

    def clean(self) -> None:
        # The field skips empty values, so the mapping check runs here.
        if not isinstance(self.snapshot, dict):
            raise ValidationError({"snapshot": _SNAPSHOT_NOT_MAPPING})

    def delete(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise EngineSnapshotWriteRefused


class ReplayReason(models.TextChoices):
    """Why a replay was requested.

    The ``MatchChange`` values (``apps.matches.signals``), plus ``late``
    (a match played before a player's last match) and ``replay_needed``
    (a live update that could not be queued or failed in a worker).
    """

    VOID = "void", "Void"
    EDIT = "edit", "Edit"
    DISPUTE = "dispute", "Dispute"
    HOLD = "hold", "Hold"
    RELEASE = "release", "Release"
    LATE = "late", "Late match"
    REPLAY_NEEDED = "replay_needed", "Replay needed"


class RatingReplayRequest(models.Model):
    """A request for a replay of the Ledger (#47).

    Inserted only through ``apps.ratings.services.request_replay()``, and
    deleted only by the replay (``replay_ratings()``) and the recompute
    (``recompute_ratings()``, #48), each of which takes every pending
    request, so quick successive changes share one replay.
    ``earliest_date`` is the earliest date the change can affect, kept for
    logging only: every replay is a full replay. Names no Player and holds
    no personal data.
    """

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    earliest_date = models.DateField(error_messages=_DATE_MESSAGES)
    reason = models.CharField(
        max_length=16, choices=ReplayReason, error_messages=CHOICE_MESSAGES
    )

    class Meta:
        ordering = ("created_at", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(reason__in=ReplayReason.values),
                name="ratings_ratingreplayrequest_reason_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reason} from {self.earliest_date.isoformat()}"
