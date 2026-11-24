"""The rating receiver catches broker errors only, not the task's own (#46).

Tasks run eagerly in tests, so ``delay()`` runs the live update in the
caller. A failing live update must raise there, not be logged as a
failure to queue, or tests that confirm matches would hide it. Made-up
data only.
"""

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest

from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match
from apps.players.models import GenderDivision
from apps.ratings import services
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


class MadeUpFailure(Exception):
    pass


def test_a_failing_live_update_is_not_hidden_as_a_queue_failure(
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
    )

    def fail(match_id: int) -> str:
        raise MadeUpFailure

    monkeypatch.setattr(services, "update_ratings_for_match", fail)

    def confirm_by_both() -> None:
        with django_capture_on_commit_callbacks(execute=True):
            confirm_match(match, actor=players[Team.A][0].account)
            confirm_match(
                Match.objects.get(pk=match.pk), actor=players[Team.B][0].account
            )

    with caplog.at_level(logging.ERROR), pytest.raises(MadeUpFailure):
        confirm_by_both()

    assert not any("Could not queue" in r.getMessage() for r in caplog.records)
