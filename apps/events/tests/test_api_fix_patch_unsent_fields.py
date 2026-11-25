"""A kind-only change is not refused for a field it did not send (#125).

An Event stored outside the services can hold a name that is not in the
model's cleaned form (surrounding spaces). Cleaning it while checking a
PATCH must not count as a name change: only the fields sent are compared.
Made-up data only.
"""

from datetime import date

import pytest
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def test_kind_only_patch_on_matched_event_with_uncleaned_name() -> None:
    organizer = verified_organizer()
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=account)
    event = EventFactory.create(
        organizer=organizer,
        name="  Made-up Spaced Ladder  ",
        kind=EventKind.TOURNAMENT,
        start_date=date(2026, 3, 1),
        end_date=date(2026, 3, 31),
    )
    assert Event.objects.get(pk=event.pk).name == "  Made-up Spaced Ladder  "
    record_match(
        format=MatchFormat.SINGLES,
        team_a=[PlayerFactory.create()],
        team_b=[PlayerFactory.create()],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 3, 2),
        submitted_by_user=account,
        event=event,
    )
    client = Client()
    client.force_login(account)

    response = client.patch(
        f"/api/v1/organizer/events/{event.public_code}",
        {"kind": "league"},
        content_type="application/json",
    )

    assert response.status_code == 200
    assert Event.objects.get(pk=event.pk).kind == EventKind.LEAGUE
