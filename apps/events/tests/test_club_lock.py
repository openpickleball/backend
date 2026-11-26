"""An Event's club cannot change once a Match names it (#163, PR #280).

Like the organizer, the club is fixed once any Match (voided included)
names the Event: ``Event.save()`` compares it with the stored value under
its row lock, and ``clean()`` reports the same fixed message. A save
whose ``update_fields`` leave out the club writes none, so it is not
refused. Made-up data only.
"""

import pytest
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import Club
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.events.models import Event
from apps.events.services import save_event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchStatus
from apps.matches.tests.factories import MatchFactory
from apps.matches.workflow import void_match

pytestmark = pytest.mark.django_db

LOCKED = "This event has matches, so its club cannot change."


def _granted_club(event: Event) -> Club:
    club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    return club


def _event_at_club() -> Event:
    event: Event = EventFactory.create()
    event.club = _granted_club(event)
    event.save()
    return event


def _stored_club_id(event: Event) -> int | None:
    stored: int | None = Event.objects.values_list("club_id", flat=True).get(
        pk=event.pk
    )
    return stored


def test_save_refuses_a_club_change_once_a_match_names_the_event() -> None:
    event = _event_at_club()
    MatchFactory.create(event=event)
    old = event.club_id
    event.club = _granted_club(event)

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"club": [LOCKED]}
    assert _stored_club_id(event) == old


def test_removing_the_club_is_refused_too() -> None:
    event = _event_at_club()
    MatchFactory.create(event=event)
    event.club = None

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"club": [LOCKED]}


def test_an_event_whose_only_match_was_voided_still_refuses() -> None:
    event = _event_at_club()
    verify(event.organizer)
    match = MatchFactory.create(event=event)
    member = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    void_match(match, actor=member)
    assert Match.objects.get(pk=match.pk).status == MatchStatus.VOIDED
    event.club = _granted_club(event)

    with pytest.raises(ValidationError) as caught:
        event.save()

    assert caught.value.message_dict == {"club": [LOCKED]}


def test_a_club_change_with_no_match_is_allowed() -> None:
    event = _event_at_club()
    new = _granted_club(event)
    event.club = new

    event.save()

    assert _stored_club_id(event) == new.pk


def test_update_fields_without_the_club_are_not_refused() -> None:
    event = _event_at_club()
    MatchFactory.create(event=event)
    event.club = _granted_club(event)
    event.name = "Made-up Renamed Cup"
    old = _stored_club_id(event)

    event.save(update_fields=["name"])

    assert _stored_club_id(event) == old
    assert Event.objects.get(pk=event.pk).name == "Made-up Renamed Cup"


def test_a_stale_instance_never_writes_its_old_club_back() -> None:
    stale = _event_at_club()
    new = _granted_club(stale)
    QuerySet(model=Event).filter(pk=stale.pk).update(club=new)
    MatchFactory.create(event=Event.objects.get(pk=stale.pk))
    stale.name = "Made-up Renamed Cup"

    with pytest.raises(ValidationError) as caught:
        stale.save()

    assert caught.value.message_dict == {"club": [LOCKED]}
    assert _stored_club_id(stale) == new.pk


def test_full_clean_reports_the_same_message() -> None:
    event = _event_at_club()
    MatchFactory.create(event=event)
    event.club = _granted_club(event)

    with pytest.raises(ValidationError) as caught:
        event.full_clean()

    assert caught.value.message_dict["club"] == [LOCKED]


def test_save_event_refuses_and_records_nothing() -> None:
    event = _event_at_club()
    MatchFactory.create(event=event)
    event.club = _granted_club(event)

    with pytest.raises(ValidationError):
        save_event(event, actor=UserFactory.create(is_staff=True))

    assert not AuditEvent.objects.filter(target_object_id=str(event.pk)).exists()


def test_the_admin_form_shows_the_message(client: Client) -> None:
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    event = _event_at_club()
    MatchFactory.create(event=event)
    old = event.club_id
    other = _granted_club(event)

    response = client.post(
        reverse("admin:events_event_change", args=[event.pk]),
        {
            "name": event.name,
            "kind": event.kind,
            "organizer": str(event.organizer_id),
            "club": str(other.pk),
            "start_date": event.start_date.isoformat(),
            "end_date": event.end_date.isoformat(),
        },
    )

    assert response.status_code == 200
    assert response.context["adminform"].form.errors["club"] == [LOCKED]
    assert _stored_club_id(event) == old
