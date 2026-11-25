"""The data migration fills each existing Match's match type (#163).

The test runs inside the test transaction, which rolls back, so the schema
it migrates back and forward ends at the latest migration again. Postgres
runs schema changes inside a transaction.
"""

from collections.abc import Iterator

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.clubs.tests.factories import ClubFactory
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match
from apps.matches.tests.factories import MatchFactory

pytestmark = pytest.mark.django_db

BEFORE_FILL = [("matches", "0009_match_match_type")]
AFTER_FILL = [("matches", "0011_match_match_type_required")]


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    """A migration executor that leaves the schema at the latest migration."""
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def test_fill_sets_each_match_type_from_its_event_or_club(
    executor: MigrationExecutor,
) -> None:
    tournament = MatchFactory.create(
        event=EventFactory.create(kind=EventKind.TOURNAMENT)
    )
    league = MatchFactory.create(event=EventFactory.create(kind=EventKind.LEAGUE))
    rec = MatchFactory.create(event=EventFactory.create(kind=EventKind.REC_VERIFIED))
    club_only = MatchFactory.create(club=ClubFactory.create())
    self_reported = MatchFactory.create()
    # Deferred foreign key checks would block the schema changes below.
    with connection.cursor() as cursor:
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    executor.migrate(BEFORE_FILL)
    old_apps = executor.loader.project_state(BEFORE_FILL).apps
    # The historical model has a plain manager, with no ledger guard.
    old_apps.get_model("matches", "Match").objects.update(match_type=None)

    executor.loader.build_graph()
    executor.migrate(AFTER_FILL)

    stored = dict(Match.objects.values_list("pk", "match_type"))
    assert stored == {
        tournament.pk: "tournament",
        league.pk: "league",
        rec.pk: "rec_verified",
        club_only.pk: "rec_verified",
        self_reported.pk: "self_reported",
    }
