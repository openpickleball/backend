"""A replay run with no request pending takes no lock (#47). Made-up data only.

The beat entry runs the replay task every 15 minutes. With no request
pending it must not lock the engine snapshot row, which would make every
live update wait behind it for nothing.
"""

from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.ratings import services
from apps.ratings.services import ReplayOutcome, replay_ratings

pytestmark = pytest.mark.django_db


def _fail(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("The no-op path must not reach the snapshot row.")


def test_nothing_requested_never_reaches_the_snapshot_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "live_config", _fail)
    monkeypatch.setattr(services, "_snapshot_row", _fail)

    assert replay_ratings() == ReplayOutcome.NOTHING_REQUESTED


def test_nothing_requested_runs_no_locking_query() -> None:
    with CaptureQueriesContext(connection) as queries:
        assert replay_ratings() == ReplayOutcome.NOTHING_REQUESTED

    for query in queries.captured_queries:
        assert "FOR UPDATE" not in query["sql"].upper()
        assert "INSERT" not in query["sql"].upper()
