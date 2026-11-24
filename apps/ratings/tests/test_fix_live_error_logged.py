"""A live update that fails logs the Match id and error class only (#46).

An engine or apply error propagates, so the task fails visibly, but first
the service logs one line with the Match's row id and the error's class,
never the error's text, which can quote values. Made-up data only.
"""

import logging
from typing import Any

import pytest

from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import MatchFormat, Team
from apps.players.models import GenderDivision
from apps.ratings import services
from apps.ratings.engine_adapter import EngineMappingError
from apps.ratings.services import update_ratings_for_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
DETAIL = "made-up detail that must not be logged"


def _raise(error: Exception) -> Any:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("record_live", EngineMappingError(DETAIL)),
        ("apply_rating", ValueError(DETAIL)),
    ],
)
def test_a_failure_is_logged_by_id_and_class_then_raised(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    target: str,
    error: Exception,
) -> None:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES, event=event
    )
    monkeypatch.setattr(services, target, _raise(error))

    with (
        caplog.at_level(logging.ERROR, logger="apps.ratings.services"),
        pytest.raises(type(error)),
    ):
        update_ratings_for_match(match.pk)

    records = [r for r in caplog.records if r.name == "apps.ratings.services"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert str(match.pk) in message
    assert type(error).__name__ in message
    assert DETAIL not in message
    assert records[0].exc_info is None
