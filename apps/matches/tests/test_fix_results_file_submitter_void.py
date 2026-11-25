"""A moderator's void of a submitter's matches stops that submitter's
pending uploads (#71).

Every pending Results File of the submitter fails as ``voided`` before
any match is read, so a run in flight stops at its next row and a queued
file records nothing. A refused void touches no file. Made-up data only.
"""

from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from apps.matches import results_files, tasks
from apps.matches.models import Match, MatchStatus, ResultsFile, ResultsFileStatus
from apps.matches.permissions import MatchActionDenied
from apps.matches.results_files import process_results_file
from apps.matches.tests.results_file_builders import (
    moderator,
    organizer_member,
    row,
    upload,
)
from apps.matches.workflow import void_submitter_matches
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _two_rows() -> tuple[ResultsFile, Any]:
    event, member = organizer_member()
    players = [PlayerFactory.create() for _ in range(4)]
    results_file = upload(
        member,
        event,
        [row(players[0], players[1], 7), row(players[2], players[3], 5)],
    )
    return results_file, member


def test_a_queued_file_of_the_submitter_records_nothing() -> None:
    results_file, member = _two_rows()

    void_submitter_matches(
        member,
        recorded_since=timezone.now().date(),
        actor=moderator(),
        reason=REASON,
    )
    process_results_file(results_file.pk)

    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.FAILED
    assert stored.error_code == "voided"
    assert bytes(stored.content) == b""
    assert not Match.objects.exists()


def test_a_run_in_flight_stops_once_the_submitter_is_voided() -> None:
    results_file, member = _two_rows()
    acting = moderator()
    real_record_match = results_files.record_match
    calls: list[int] = []

    def record_then_void(**values: Any) -> Any:
        recorded = real_record_match(**values)
        calls.append(values["sequence"])
        if len(calls) == 1:
            void_submitter_matches(
                member,
                recorded_since=timezone.now().date(),
                actor=acting,
                reason=REASON,
            )
        return recorded

    with mock.patch.object(results_files, "record_match", record_then_void):
        process_results_file(results_file.pk)

    assert calls == [1]
    assert Match.objects.count() == 1
    assert not Match.objects.exclude(status=MatchStatus.VOIDED).exists()
    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.FAILED
    assert stored.error_code == "voided"


def test_another_submitters_file_is_untouched() -> None:
    results_file, _member = _two_rows()
    _other_file, other = _two_rows()

    void_submitter_matches(
        other,
        recorded_since=timezone.now().date(),
        actor=moderator(),
        reason=REASON,
    )

    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.PENDING


def test_a_refused_void_leaves_the_file_pending() -> None:
    results_file, member = _two_rows()

    with pytest.raises(MatchActionDenied):
        void_submitter_matches(
            member,
            recorded_since=timezone.now().date(),
            actor=member,
            reason=REASON,
        )

    assert (
        ResultsFile.objects.get(pk=results_file.pk).status == ResultsFileStatus.PENDING
    )
