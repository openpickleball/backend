"""The Organizer's Event operations at /api/v1/organizer/events (#125).

A signed-in Account lists the Events of every Organizer it is a member of
(either role, verified or not). A member of a verified Organizer
(``apps.matches.permissions.can_manage_events``, ADR 0021) creates,
changes, and deletes its Events, through ``apps.events.services``, which
record one Audit Event each with the Account as actor.

Create names the Organizer in the body: an unknown or malformed code, an
unverified Organizer, and an Account that is not a member all get one
403 with fixed text, before the Club is read and before anything is
written. Change and delete name the Event in the path: anyone who may not
manage it gets the same 404 as an unknown or malformed code (ADR 0008,
ADR 0015).

Change and delete check the permission without a lock, so a refused
actor never locks the Event, then lock the row ``FOR UPDATE`` in their
transaction and check again, so they serialize with a match in flight
(which holds ``FOR KEY SHARE`` on it) and a concurrent delete gives 404. ``kind`` may
change at any time (ADR 0035). ``name`` and the dates change only while no
Match, in any status, names the Event (409 otherwise). An Event that a
Match or Submission Batch names cannot be deleted (409).
"""

from typing import TYPE_CHECKING, Annotated, Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, QuerySet
from django.http import Http404, HttpRequest
from ninja import Field, Router, Schema, Status
from ninja.errors import HttpError
from ninja.errors import ValidationError as NinjaValidationError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.clubs.models import Club, Organizer, OrganizerMember
from apps.events.models import Event
from apps.events.schemas import (
    EventCreateIn,
    EventOut,
    EventUpdateIn,
    event_out,
)
from apps.events.services import delete_event, save_event
from apps.matches.api import request_account
from apps.matches.permissions import can_manage_events
from apps.public_ids.api import PublicCodePath, get_by_public_code
from apps.public_ids.codes import CodeType, normalize
from config.api_errors import ErrorResponse, model_validation_error
from config.auth import account_auth
from config.throttling import account_throttle

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

router = Router(tags=["events"], auth=account_auth, throttle=account_throttle)

EventCode = Annotated[str, PublicCodePath(CodeType.EVENT)]

PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
# Postgres takes a bigint OFFSET: a larger one would fail in the database.
MAX_OFFSET = 2**63 - 1

# Fixed texts, never a submitted value.
FORBIDDEN_MESSAGE = "You may not manage this organizer's events."
LOCKED_MESSAGE = "This event has matches, so its name and dates cannot change."
PROTECTED_MESSAGE = "This event has matches or uploads, so it cannot be deleted."
NOT_SAVED_MESSAGE = "The event could not be saved. Try again."
# The model's own message for a Club that does not exist.
UNKNOWN_CLUB_MESSAGE = str(Event._meta.get_field("club").error_messages["invalid"])
# The fields fixed once a Match names the Event, besides the organizer and
# club, which this API never changes.
_FIXED_ONCE_MATCHED = ("name", "start_date", "end_date")


class EventPagination(LimitOffsetPagination):
    """Limit and offset paging of Events, with bounded limit and offset.

    Each page's Events become ``EventOut``.
    """

    class Input(Schema):
        limit: int = Field(PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
        offset: int = Field(0, ge=0, le=MAX_OFFSET)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(max_limit=MAX_PAGE_SIZE, **kwargs)

    def paginate_queryset(
        self,
        queryset: QuerySet[Event],
        pagination: Any,
        request: HttpRequest,
        **params: Any,
    ) -> Any:
        page = super().paginate_queryset(queryset, pagination, request, **params)
        page[self.items_attribute] = [
            event_out(event) for event in page[self.items_attribute]
        ]
        return page


@router.get(
    "/events",
    response={200: list[EventOut]},
    operation_id="list_organizer_events",
)
@paginate(EventPagination)
def list_organizer_events(request: HttpRequest) -> QuerySet[Event]:
    """The Events of every Organizer the signed-in Account is a member of.

    Either role, verified or not. Newest start date first.
    """
    account = request_account(request)
    organizer_ids = OrganizerMember.objects.filter(account=account).values(
        "organizer_id"
    )
    return (
        Event.objects.filter(organizer_id__in=organizer_ids)
        .select_related("organizer", "club")
        .order_by("-start_date", "-pk")
    )


def _club(raw: str | None) -> Club | None:
    """The Club ``raw`` names, None for no Club, or a 422 on ``body.club``."""
    if raw is None:
        return None
    code = normalize(raw, CodeType.CLUB)
    club = Club.objects.filter(public_code=code).first() if code else None
    if club is None:
        raise NinjaValidationError(
            [{"loc": ("body", "club"), "type": "invalid", "msg": UNKNOWN_CLUB_MESSAGE}]
        )
    return club


@router.post(
    "/events",
    response={201: EventOut, 403: ErrorResponse, 409: ErrorResponse},
    operation_id="create_organizer_event",
)
def create_organizer_event(
    request: HttpRequest, payload: EventCreateIn
) -> Status[EventOut]:
    """Create an Event for a verified Organizer the Account is a member of.

    403 for any other Organizer, an unknown one included. A Club must have
    granted the Organizer a Club Organizer Grant (422 on ``club``).
    """
    account = request_account(request)
    code = normalize(payload.organizer, CodeType.ORGANIZER)
    organizer = Organizer.objects.filter(public_code=code).first() if code else None
    if organizer is None or not can_manage_events(account, organizer.pk):
        raise HttpError(403, FORBIDDEN_MESSAGE)
    event = Event(
        name=payload.name,
        kind=payload.kind,
        organizer=organizer,
        club=_club(payload.club),
        start_date=payload.start_date,
        end_date=payload.end_date,
    )
    try:
        with transaction.atomic():
            save_event(event, actor=account)
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    except IntegrityError:
        # Foreign keys are checked at commit: the Organizer or Club was
        # deleted after save_event's checks. Caught outside the
        # transaction, which rolled back with the Audit Event.
        raise HttpError(409, NOT_SAVED_MESSAGE) from None
    except ValueError:
        # Defensive: save_event refuses a related record or Event that is
        # not stored. Never a 500.
        raise HttpError(409, NOT_SAVED_MESSAGE) from None
    return Status(201, event_out(event))


def _managed_event(account: "_User", code: str) -> Event:
    """The Event, locked ``FOR UPDATE``, when ``account`` may manage it.

    404 for an unknown Event and for one the Account may not manage alike.
    The permission is checked first without a lock, so a refused actor
    never locks the row (which would block the Organizer's in-flight match
    submissions), then again on the locked row, whose Organizer cannot
    change while it is held. Call inside a transaction.
    """
    found = get_by_public_code(Event.objects.only("pk", "organizer_id"), code)
    if not can_manage_events(account, found.organizer_id):
        raise Http404
    event = Event.objects.select_for_update().filter(pk=found.pk).first()
    if event is None or not can_manage_events(account, event.organizer_id):
        raise Http404
    return event


@router.patch(
    "/events/{code}",
    response={200: EventOut, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="update_organizer_event",
)
def update_organizer_event(
    request: HttpRequest, code: EventCode, payload: EventUpdateIn
) -> EventOut:
    """Change an Event's name, kind, or dates. Only the fields sent change.

    The kind may change at any time and reaches only matches recorded
    after it. The name and dates change only while no match names the
    Event (409 otherwise); sending the stored value is not a change.
    """
    account = request_account(request)
    changes = payload.model_dump(exclude_unset=True)
    try:
        with transaction.atomic():
            event = _managed_event(account, code)
            stored = {name: getattr(event, name) for name in _FIXED_ONCE_MATCHED}
            for name, value in changes.items():
                setattr(event, name, value)
            # The model's own cleaning (the name is stripped), so a sent
            # value equal to the stored one is not a change.
            event.full_clean(exclude=["public_code"])
            changed = any(
                getattr(event, name) != stored[name] for name in _FIXED_ONCE_MATCHED
            )
            if changed and event.matches.exists():
                raise HttpError(409, LOCKED_MESSAGE)
            save_event(event, actor=account)
    except DjangoValidationError as error:
        raise model_validation_error(error) from None
    except ValueError:
        raise Http404 from None
    return event_out(event)


@router.delete(
    "/events/{code}",
    response={204: None, 404: ErrorResponse, 409: ErrorResponse},
    operation_id="delete_organizer_event",
)
def delete_organizer_event(request: HttpRequest, code: EventCode) -> Status[None]:
    """Delete an Event. 409 when a match or an upload names it."""
    account = request_account(request)
    try:
        with transaction.atomic():
            delete_event(_managed_event(account, code), actor=account)
    except ProtectedError:
        # Outside the transaction, which rolled back with the Audit Event.
        raise HttpError(409, PROTECTED_MESSAGE) from None
    return Status(204, None)
