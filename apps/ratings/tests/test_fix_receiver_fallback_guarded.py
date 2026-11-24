"""A failed replay request after a broker error never fails a confirmation (#47).

When the broker refuses the live update, the receiver stores a replay
request instead, after the confirmation committed. If storing it fails
too (a database error during the same outage), the error must not
escape the on_commit callback: the confirmation already succeeded, and
Django would skip the commit's remaining callbacks. It is logged with
the Match id and error class only. Made-up data only.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest
from django.db import DatabaseError, transaction
from kombu.exceptions import OperationalError

from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.workflow import confirm_match
from apps.players.models import GenderDivision
from apps.ratings import receivers, tasks
from apps.ratings.models import RatingReplayRequest
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db

Capture = Callable[..., AbstractContextManager[list[Callable[[], None]]]]
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
SECRET = "made-up secret detail"


def test_a_failed_fallback_request_is_logged_and_later_callbacks_run(
    django_capture_on_commit_callbacks: Capture,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    match, players = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        status=MatchStatus.PENDING,
    )

    def broker_down(*args: object) -> None:
        raise OperationalError("made-up broker outage")

    def database_down(*args: object, **kwargs: object) -> None:
        raise DatabaseError(SECRET)

    monkeypatch.setattr(tasks.update_ratings_for_match, "delay", broker_down)
    monkeypatch.setattr(receivers, "request_replay", database_down)
    ran: list[str] = []

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        confirm_match(match, actor=players[Team.A][0].account)
        confirm_match(Match.objects.get(pk=match.pk), actor=players[Team.B][0].account)
        transaction.on_commit(lambda: ran.append("later"))

    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert ran == ["later"]
    assert not RatingReplayRequest.objects.exists()
    lines = [r for r in caplog.records if r.name == "apps.ratings.receivers"]
    assert any(
        str(match.pk) in r.getMessage() and "DatabaseError" in r.getMessage()
        for r in lines
    )
    for record in caplog.records:
        assert SECRET not in record.getMessage()
        assert record.exc_info is None
