"""Events: tournaments, leagues, and verified rec sessions an Organizer runs.

An Event groups the matches played under one Organizer, optionally at a
Club that granted that Organizer a Club Organizer Grant (ADR 0021), over a
span of dates. Its ``kind`` is the match type of the matches recorded
with it, and a kind change reaches only matches recorded after it.
Self-reported matches have no Event (decision F1 in #1).

Each Match stores the kind as its match type when it is recorded, so the
kind may change at any time and the change reaches only matches recorded
after it. The organizer decides who may submit the Event's counted
matches and the club consents to them, so neither can change once any
Match (in any status) names the Event (ADR 0035).

An Event has no weight of its own. The engine weights a match only by its
match type and takes no per-event weight (openpickleball/engine#30), so
any weight applied here would be rating math outside the engine
(CLAUDE.md, hard rule 2).

#125 adds the Event API.
"""

import datetime
from typing import Any, ClassVar

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Q

from apps.clubs.models import ClubOrganizerGrant, _CleanedTextModel
from apps.public_ids.codes import CodeType

# Messages never include the submitted value (CLAUDE.md, API). Django's
# defaults for these keys do.
CHOICE_MESSAGES = {"invalid_choice": "Choose one of the listed options."}
_DATE_MESSAGE = "Enter a valid date in YYYY-MM-DD format."
_DATE_MESSAGES = {"invalid": _DATE_MESSAGE, "invalid_date": _DATE_MESSAGE}
_DATES_ORDER = "The end date cannot be before the start date."
# Django's default names the submitted row id.
_ORGANIZER_MESSAGES = {"invalid": "Choose an existing organizer."}
_CLUB_MESSAGES = {"invalid": "Choose an existing club."}
_CLUB_NOT_GRANTED = "This club has not allowed the organizer to name it."
ORGANIZER_LOCKED = "This event has matches, so its organizer cannot change."
CLUB_LOCKED = "This event has matches, so its club cannot change."
# The fields fixed once a Match names the Event, with their columns.
_LOCKED_COLUMNS = {"organizer": "organizer_id", "club": "club_id"}
_LOCKED_MESSAGES = {"organizer": ORGANIZER_LOCKED, "club": CLUB_LOCKED}


class EventKind(models.TextChoices):
    """The match type of matches recorded with an Event.

    Each Match stores the kind it was recorded with, so a kind change
    reaches only matches recorded after it (ADR 0035).

    The values are the engine's ``open_engine.MatchType`` values.
    ``self_reported`` is left out: self-reported matches have no Event
    (decision F1 in #1). Never add a value the engine lacks.
    """

    TOURNAMENT = "tournament", "Tournament"
    LEAGUE = "league", "League"
    REC_VERIFIED = "rec_verified", "Verified rec play"


class Event(_CleanedTextModel):
    """A tournament, league, or verified rec session (CONTEXT.md, Event)."""

    public_code_type = CodeType.EVENT
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("name",)

    name = models.CharField(max_length=200)
    kind = models.CharField(
        max_length=20, choices=EventKind, error_messages=CHOICE_MESSAGES
    )
    organizer = models.ForeignKey(
        "clubs.Organizer",
        on_delete=models.PROTECT,
        related_name="events",
        error_messages=_ORGANIZER_MESSAGES,
    )
    club = models.ForeignKey(
        "clubs.Club",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="events",
        error_messages=_CLUB_MESSAGES,
    )
    start_date = models.DateField(error_messages=_DATE_MESSAGES)
    end_date = models.DateField(error_messages=_DATE_MESSAGES)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                # Refuses "" and whitespace only.
                condition=~Q(name__regex=r"^\s*$"),
                name="events_event_name_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=EventKind.values),
                name="events_event_kind_valid",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gte=F("start_date")),
                name="events_event_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never the name, for one rule with Club and Organizer.
        return self.public_code

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, refusing an organizer or club change once a Match names it.

        Every save of an existing Event first locks its row with a plain
        ``FOR UPDATE`` and compares the organizer and club with the stored
        ones under that lock, so a stale instance never writes an old value
        back over another session's change. Every Match write transaction
        for an Event (``record_match`` and ``edit_match``, through
        ``apps.matches.services.hold_event``) first takes ``FOR KEY SHARE``
        on the row and checks the submitter again (``record_match`` also
        reads the kind), so the two serialize: this save waits until an
        in-flight submission commits (an organizer or club change then
        sees its Match and refuses), or the submission waits until this
        save commits and is judged against the stored Event. The check
        counts Matches in every status, voided ones included. A save whose
        ``update_fields`` leave out a field writes none of it, so it is not
        refused for that field, but it still takes the lock. Raw writes
        (``QuerySet.update``, raw SQL) skip all of this.
        """
        if self._state.adding or self.pk is None:
            super().save(*args, **kwargs)
            return
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            # Read a one-pass iterator once, and hand Django the same values.
            update_fields = tuple(update_fields)
            kwargs["update_fields"] = update_fields
        with transaction.atomic():
            stored = (
                Event._base_manager.select_for_update()
                .filter(pk=self.pk)
                .values(*_LOCKED_COLUMNS.values())
                .first()
            )
            errors = self._locked_field_errors(stored, update_fields)
            if errors:
                raise ValidationError(errors)
            super().save(*args, **kwargs)

    def clean(self) -> None:
        # Django runs clean() even when a field failed, so a date may still
        # be the raw input here. A form clears a date that failed first
        # (EventAdminForm), since it leaves that field at its stored value.
        start, end = self.start_date, self.end_date
        if (
            isinstance(start, datetime.date)
            and isinstance(end, datetime.date)
            and end < start
        ):
            raise ValidationError({"end_date": _DATES_ORDER})
        if self._club_needs_grant():
            raise ValidationError({"club": _CLUB_NOT_GRANTED})
        if not self._state.adding and self.pk is not None:
            # Without a lock, for the form's message; save() checks again
            # under its row lock.
            stored = (
                Event._base_manager.filter(pk=self.pk)
                .values(*_LOCKED_COLUMNS.values())
                .first()
            )
            errors = self._locked_field_errors(stored, None)
            if errors:
                raise ValidationError(errors)

    def _locked_field_errors(
        self, stored: dict[str, Any] | None, update_fields: tuple[str, ...] | None
    ) -> dict[str, str]:
        """The fixed message for each locked field this save would change.

        A field changes when the save writes it (no ``update_fields``, or
        ``update_fields`` naming it) and its value differs from ``stored``.
        Locked fields change freely while no Match names the Event.
        """
        if stored is None:
            return {}
        changed = []
        for name, column in _LOCKED_COLUMNS.items():
            value = getattr(self, column)
            if value is not None and not isinstance(value, int):
                # Field validation reports an invalid link.
                continue
            writes = (
                update_fields is None
                or name in update_fields
                or column in update_fields
            )
            if writes and value != stored[column]:
                changed.append(name)
        if not changed or not self._has_matches():
            return {}
        return {name: _LOCKED_MESSAGES[name] for name in changed}

    def _has_matches(self) -> bool:
        """True when any Match, in any status, names this Event."""
        # The reverse relation, since apps.matches imports this module.
        return bool(self.matches.exists())

    def _club_needs_grant(self) -> bool:
        """True when the Club has not granted the Organizer (ADR 0021).

        A Club decides who files under its name, so a new Event, or a
        change of its Club or Organizer, needs a Club Organizer Grant from
        the Club to the Organizer. An existing Event whose stored Club and
        Organizer are unchanged is not checked again, so a Club that
        removes its grant later does not block an unrelated edit. The
        stored values and the grant are read from the database.
        """
        if not isinstance(self.club_id, int) or not isinstance(self.organizer_id, int):
            # No Club, or field validation reports a missing or invalid link.
            return False
        if self.pk is not None:
            stored = (
                Event.objects.filter(pk=self.pk)
                .values("club_id", "organizer_id")
                .first()
            )
            if stored == {"club_id": self.club_id, "organizer_id": self.organizer_id}:
                return False
        return not ClubOrganizerGrant.objects.filter(
            club_id=self.club_id, organizer_id=self.organizer_id
        ).exists()
