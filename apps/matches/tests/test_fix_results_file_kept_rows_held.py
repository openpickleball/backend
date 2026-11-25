"""A file that ends early keeps the held-match warning on its stored rows
(#71).

When the sweep expires a partly processed file, or a void fails it, the
row results it keeps get the same ``possible_duplicate`` warning a
finished file shows: every recorded row whose match is confirmed but
held. Made-up data only.
"""

from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from apps.matches import results_files, tasks
from apps.matches.models import ResultsFile, ResultsFileStatus
from apps.matches.results_files import (
    PENDING_EXPIRY,
    REQUEUE_AFTER,
    fail_for_void,
    process_results_file,
    sweep_results_files,
)
from apps.matches.tests.results_file_builders import organizer_member, row, upload
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _stopped_pair() -> ResultsFile:
    """A held pair recorded by a run that stopped before it finished."""
    event, member = organizer_member()
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 7), row(a, b, 5)])
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)
    return results_file


def _codes(results_file: ResultsFile) -> list[list[str]]:
    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.FAILED
    return [[item["code"] for item in result["warnings"]] for result in stored.report]


def test_an_expired_files_held_rows_get_the_warning() -> None:
    results_file = _stopped_pair()
    now = timezone.now()
    ResultsFile.objects.filter(pk=results_file.pk).update(
        created_at=now - PENDING_EXPIRY - timedelta(minutes=1),
        queued_at=now - REQUEUE_AFTER - timedelta(minutes=1),
    )

    sweep_results_files()

    assert _codes(results_file) == [["possible_duplicate"], ["possible_duplicate"]]


def test_a_voided_files_held_rows_get_the_warning() -> None:
    results_file = _stopped_pair()

    fail_for_void(results_file.submission_batch_id)

    assert _codes(results_file) == [["possible_duplicate"], ["possible_duplicate"]]
