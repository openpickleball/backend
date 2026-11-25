"""The API's request and response schemas for Events (#125).

No schema here carries a row id: an Event, its Organizer, and its Club
are named by their OPEN Codes. Unknown keys are refused.
"""

from datetime import date

from ninja import Schema
from pydantic import ConfigDict, field_validator
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from apps.events.models import Event, EventKind


class EventCreateIn(Schema):
    """A new Event for a verified Organizer the signed-in Account is a
    member of.

    ``organizer`` and ``club`` are OPEN Codes, in any case, with or
    without hyphens. The Club, when named, must have granted the
    Organizer a Club Organizer Grant (ADR 0021).
    """

    model_config = ConfigDict(extra="forbid")

    organizer: str
    name: str
    kind: EventKind
    club: str | None = None
    start_date: date
    end_date: date


class EventUpdateIn(Schema):
    """Changes to an Event. Only the fields sent change; null is refused.

    ``kind`` may change at any time and reaches only matches recorded
    after it (ADR 0035). ``name``, ``start_date``, and ``end_date`` change
    only while no match names the Event. The organizer and club cannot be
    changed here.
    """

    model_config = ConfigDict(extra="forbid")

    # None only as the default of a key left out (pydantic does not validate
    # defaults), so the document shows no null; an explicit null is refused.
    name: str | SkipJsonSchema[None] = None
    kind: EventKind | SkipJsonSchema[None] = None
    start_date: date | SkipJsonSchema[None] = None
    end_date: date | SkipJsonSchema[None] = None

    @field_validator("name", "kind", "start_date", "end_date", mode="before")
    @classmethod
    def _refuse_null(cls, value: object) -> object:
        if value is None:
            raise PydanticCustomError("null", "This field cannot be null.")
        return value


class EventOut(Schema):
    """An Event, with its Organizer and Club named by OPEN Code."""

    code: str
    name: str
    kind: EventKind
    organizer: str
    club: str | None
    start_date: date
    end_date: date


def event_out(event: Event) -> EventOut:
    """``event`` for the API. Load it with its ``organizer`` and ``club``."""
    return EventOut(
        code=event.public_code,
        name=event.name,
        kind=EventKind(event.kind),
        organizer=event.organizer.public_code,
        club=event.club.public_code if event.club is not None else None,
        start_date=event.start_date,
        end_date=event.end_date,
    )
