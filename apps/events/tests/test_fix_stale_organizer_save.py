"""A stale Event instance never writes its old organizer back (#163).

``Event.save()`` compares the organizer with the stored one under the row
lock, so an instance loaded before another session's organizer change
cannot restore the old organizer over a match recorded under the new one.
A save with ``update_fields`` that leaves the organizer out writes no
organizer, so it is not refused, but it still takes the lock. Made-up data
only.
"""

import contextlib
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.services import record_match
from apps.matches.tests.factories import MatchFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

LOCKED = "This event has matches, so its organizer cannot change."


def _stale_event_with_match_under_new_organizer() -> tuple[Event, Organizer]:
    """A stale instance (old organizer) of an Event now owned by another one.

    The organizer moves by a raw write, as another session's committed
    change would look to this instance, and a match is then recorded
    under the new organizer.
    """
    stale: Event = EventFactory.create(organizer=verified_organizer())
    new = verified_organizer()
    QuerySet(model=Event).filter(pk=stale.pk).update(organizer=new)
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=new, account=account)
    players = [PlayerFactory.create() for _ in range(2)]
    record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7),),
        played_on=date(2026, 6, 1),
        submitted_by_user=account,
        event=Event.objects.get(pk=stale.pk),
    )
    return stale, new


def _stored_organizer_id(event: Event) -> int:
    stored: int = Event.objects.values_list("organizer_id", flat=True).get(pk=event.pk)
    return stored


def test_a_stale_save_refuses_to_write_the_old_organizer_back() -> None:
    stale, new = _stale_event_with_match_under_new_organizer()
    stale.name = "Made-up Renamed League"

    with pytest.raises(ValidationError) as caught:
        stale.save()

    assert caught.value.message_dict == {"organizer": [LOCKED]}
    assert _stored_organizer_id(stale) == new.pk


def test_a_change_committed_after_an_unlocked_read_is_never_overwritten() -> None:
    """Another session's organizer change lands after save's unlocked read.

    The change and a match under the new organizer are injected right
    after the first read of the Event row that takes no lock, as a
    concurrent commit would land. A save that reads the row only under
    its lock never gives that window, and must never write the old
    organizer back over the new one.
    """
    event: Event = EventFactory.create(organizer=verified_organizer())
    old_organizer_id = event.organizer_id
    new = verified_organizer()
    injected = {"done": False}

    def land_change_after_unlocked_read(
        execute: Any, sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        result = execute(sql, params, many, context)
        if (
            not injected["done"]
            and sql.lstrip().upper().startswith("SELECT")
            and '"events_event"' in sql.split("WHERE")[0]
            and "FOR UPDATE" not in sql
        ):
            injected["done"] = True
            QuerySet(model=Event).filter(pk=event.pk).update(organizer=new)
            MatchFactory.create(event=Event.objects.get(pk=event.pk))
        return result

    event.name = "Made-up Renamed League"
    with (
        connection.execute_wrapper(land_change_after_unlocked_read),
        contextlib.suppress(ValidationError),
    ):
        event.save()

    stored = _stored_organizer_id(event)
    assert not (injected["done"] and stored == old_organizer_id)


def test_a_save_without_the_organizer_field_locks_and_is_not_refused() -> None:
    stale, new = _stale_event_with_match_under_new_organizer()
    stale.name = "Made-up Renamed League"

    with CaptureQueriesContext(connection) as queries:
        stale.save(update_fields=["name"])

    assert Event.objects.get(pk=stale.pk).name == "Made-up Renamed League"
    assert _stored_organizer_id(stale) == new.pk
    locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
    assert locks
    assert all("NO KEY" not in sql for sql in locks)


def test_every_save_of_an_existing_event_locks_the_row() -> None:
    event: Event = EventFactory.create()
    event.name = "Made-up Other Name"

    with CaptureQueriesContext(connection) as queries:
        event.save()

    assert [q for q in queries if "FOR UPDATE" in q["sql"]]
