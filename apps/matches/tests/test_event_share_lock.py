"""The write transaction share-locks the Event first (#163, ADR 0035).

``FOR KEY SHARE`` on the Event row comes before the advisory locks and any
Match lock, so an organizer change (``FOR UPDATE``) and a submission
serialize, and the lock order holds. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat
from apps.matches.services import record_match
from apps.matches.workflow import edit_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _first_index(sqls: list[str], text: str) -> int:
    return next(i for i, sql in enumerate(sqls) if text in sql)


def test_record_and_edit_share_lock_the_event_before_any_other_lock() -> None:
    event = EventFactory.create(organizer=verified_organizer())
    account = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    players = [PlayerFactory.create() for _ in range(2)]
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "played_on": date(2026, 6, 1),
    }

    with CaptureQueriesContext(connection) as recorded:
        match = record_match(
            **values, games=((11, 7),), submitted_by_user=account, event=event
        ).match
    with CaptureQueriesContext(connection) as edited:
        edit_match(match, actor=account, **values, games=((11, 2),))

    for queries in (recorded, edited):
        sqls = [q["sql"] for q in queries]
        share = next(
            i
            for i, sql in enumerate(sqls)
            if "FOR KEY SHARE" in sql and '"events_event"' in sql
        )
        # Only the acting Account's own share lock comes first (ADR 0048).
        earlier = [sql for sql in sqls[:share] if "FOR " in sql]
        assert earlier == [sql for sql in earlier if '"accounts_user"' in sql]
        assert len(earlier) == 1
        assert share < _first_index(sqls, "pg_advisory_xact_lock")
        assert share < _first_index(sqls, "INSERT")
