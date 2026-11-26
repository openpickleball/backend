"""A CSV upload's batch expects the file's match rows (#206).

Before it records any row, processing stores the number of match rows
as the batch's ``expected_size``, once, so a large upload's matches are
flagged ``large_batch`` from the first row. They still count. Made-up
data only.
"""

from typing import Any
from unittest import mock

import pytest
from pytest_django.fixtures import Settings

from apps.matches import results_files, tasks
from apps.matches.models import (
    FlagReason,
    FlagRule,
    Match,
    MatchStatus,
    ResultsFile,
    ResultsFileStatus,
)
from apps.matches.results_files import process_results_file
from apps.matches.tests.results_file_builders import organizer_member, row, upload
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _rows(count: int) -> list[list[str]]:
    # New players on each row, so no two rows are near duplicates.
    return [
        row(PlayerFactory.create(), PlayerFactory.create(), 5) for _ in range(count)
    ]


def _large(match: Match) -> bool:
    return match.flags.filter(
        reason=FlagReason.UNUSUAL_SUBMISSION, rule=FlagRule.LARGE_BATCH
    ).exists()


def test_a_large_upload_flags_every_match(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 2
    event, member = organizer_member()
    results_file = upload(member, event, _rows(3))

    process_results_file(results_file.pk)

    batch = results_file.submission_batch
    batch.refresh_from_db()
    assert batch.expected_size == 3
    matches = list(Match.objects.filter(submission_batch=batch))
    assert len(matches) == 3
    assert all(_large(match) for match in matches)
    assert all(match.status == MatchStatus.CONFIRMED for match in matches)
    assert Match.objects.counted().filter(submission_batch=batch).count() == 3


def test_an_upload_at_the_threshold_is_not_flagged(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 3
    event, member = organizer_member()
    results_file = upload(member, event, _rows(3))

    process_results_file(results_file.pk)

    batch = results_file.submission_batch
    batch.refresh_from_db()
    assert batch.expected_size == 3
    assert not any(
        _large(match) for match in Match.objects.filter(submission_batch=batch)
    )


def test_the_size_is_stored_before_the_first_row(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1
    event, member = organizer_member()
    results_file = upload(member, event, _rows(2))
    seen: list[int | None] = []
    real = results_files.record_match

    def record(**kwargs: Any) -> Any:
        batch = kwargs["submission_batch"]
        batch.refresh_from_db()
        seen.append(batch.expected_size)
        return real(**kwargs)

    with mock.patch.object(results_files, "record_match", side_effect=record):
        process_results_file(results_file.pk)

    assert seen == [2, 2]


def test_a_rerun_keeps_the_size_stored_first(settings: Settings) -> None:
    settings.UNUSUAL_SUBMISSION_BATCH_SIZE = 1_000
    event, member = organizer_member()
    results_file = upload(member, event, _rows(2))
    # As if an earlier run stored a size and then stopped.
    results_file.submission_batch.set_expected_size(7)

    process_results_file(results_file.pk)

    batch = results_file.submission_batch
    batch.refresh_from_db()
    assert batch.expected_size == 7
    results_file.refresh_from_db()
    assert results_file.status == ResultsFileStatus.DONE
    assert Match.objects.filter(submission_batch=batch).count() == 2


def test_a_file_no_longer_pending_gets_no_size() -> None:
    event, member = organizer_member()
    results_file = upload(member, event, _rows(2))
    ResultsFile.objects.filter(pk=results_file.pk).update(
        status=ResultsFileStatus.FAILED
    )

    process_results_file(results_file.pk)

    batch = results_file.submission_batch
    batch.refresh_from_db()
    assert batch.expected_size is None


def test_an_unreadable_file_gets_no_size() -> None:
    event, member = organizer_member()
    results_file = upload(member, event, [])

    process_results_file(results_file.pk)

    batch = results_file.submission_batch
    batch.refresh_from_db()
    assert batch.expected_size is None
