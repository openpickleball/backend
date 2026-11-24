"""Events: tournaments, leagues, and verified rec sessions an Organizer runs.

An Event groups the matches played under one Organizer, optionally at a
Club that granted that Organizer a Club Organizer Grant (ADR 0021), over a
span of dates. Its ``kind`` is the match type its matches
count as. Self-reported matches have no Event (decision F1 in #1).

An Event has no weight of its own. The engine weights a match only by its
match type and takes no per-event weight (openpickleball/engine#30), so
any weight applied here would be rating math outside the engine
(CLAUDE.md, hard rule 2).

#125 adds the Event API.
"""

import datetime
from typing import ClassVar

from django.core.exceptions import ValidationError
from django.db import models
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


class EventKind(models.TextChoices):
    """The match type an Event's matches count as.

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
