"""Save and delete Events with their Audit Events (#163, ADR 0035).

An Event's organizer decides who may submit its counted matches, its
club consents to them, and its kind sets the match type of matches
recorded after a change (the organizer and club are fixed once a Match
names the Event, see ``Event.save()``), so every
create, change, and delete made through these services records one Audit
Event with an explicit actor, in the same transaction:

- ``save_event()`` runs ``full_clean()`` (``ValidationError``, nothing
  saved or recorded), then records ``event.create`` (``before`` None, ``after``
  every audited value) or ``event.change`` (``before`` and ``after`` hold
  only the changed fields), and nothing when nothing changed or the save
  raises.
- ``delete_event()`` records ``event.delete`` (``before`` every audited
  value, ``after`` None), then deletes. A refused delete (an Event a Match
  or Submission Batch names) rolls both back.

The audited values are the name, the kind, the Organizer's and Club's
OPEN Codes (read from the database by id, never from a related instance),
and the dates as ISO strings. A plain ORM save or delete, and raw writes,
record nothing. The admin uses these services; #125's API will too.
"""

from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser
from django.contrib.auth.models import AnonymousUser
from django.db import transaction

from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.events.models import Event

Actor = AbstractBaseUser | AnonymousUser | None

# The stored columns each audited value comes from.
_COLUMNS = {
    "name": "name",
    "kind": "kind",
    "organizer": "organizer__public_code",
    "club": "club__public_code",
    "start_date": "start_date",
    "end_date": "end_date",
}


def save_event(event: Event, *, actor: Actor) -> None:
    """Save ``event`` and record its create or change with ``actor``."""
    with transaction.atomic():
        before = None
        if not event._state.adding and event.pk is not None:
            before = _stored_values(event.pk, lock=True)
        # Every model rule, for a caller that did not clean the Event (the
        # admin form did): the Club Organizer Grant (ADR 0021), the dates,
        # and the organizer and club locks. The OPEN Code is issued by save().
        # The ids of related objects saved after they were assigned, so the
        # rules see the ids Django will write.
        event.copy_related_ids()
        event.full_clean(exclude=["public_code"])
        event.save()
        after = _stored_values(event.pk, lock=False)
        if after is None:
            raise RuntimeError("The saved event could not be read back.")
        if before is None:
            record_event(
                action=AuditAction.EVENT_CREATE,
                target=event,
                actor=actor,
                before=None,
                after=after,
            )
            return
        changed = [key for key in after if after[key] != before[key]]
        if changed:
            record_event(
                action=AuditAction.EVENT_CHANGE,
                target=event,
                actor=actor,
                before={key: before[key] for key in changed},
                after={key: after[key] for key in changed},
            )


def delete_event(event: Event, *, actor: Actor) -> None:
    """Record ``event``'s delete with ``actor``, then delete it.

    An Event already gone records nothing and deletes nothing.
    """
    with transaction.atomic():
        if event.pk is None:
            return
        before = _stored_values(event.pk, lock=True)
        if before is None:
            return
        record_event(
            action=AuditAction.EVENT_DELETE,
            target=event,
            actor=actor,
            before=before,
            after=None,
        )
        event.delete()


def _stored_values(pk: int, *, lock: bool) -> dict[str, Any] | None:
    """The audited values of the stored Event, or None when it is gone.

    With ``lock``, the Event row alone is locked ``FOR UPDATE`` first.
    """
    rows = Event._base_manager.filter(pk=pk)
    if lock and not list(rows.select_for_update().values_list("pk", flat=True)):
        return None
    stored = rows.values(*_COLUMNS.values()).first()
    if stored is None:
        return None
    values = {key: stored[column] for key, column in _COLUMNS.items()}
    for key in ("start_date", "end_date"):
        values[key] = values[key].isoformat()
    return values
