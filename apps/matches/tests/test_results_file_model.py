"""The Results File model (#71): a CSV file an organizer sent, held until
processed, and its report. Made-up data only."""

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    GameTarget,
    Match,
    ResultsFile,
    ResultsFileStatus,
    SubmissionBatch,
)
from apps.public_ids.codes import CodeType, normalize

pytestmark = pytest.mark.django_db


@pytest.fixture
def batch() -> SubmissionBatch:
    event: Event = EventFactory.create(organizer=verified_organizer())
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return SubmissionBatch.objects.create(submitted_by_user=account, event=event)


def test_a_results_file_gets_an_open_code(batch: SubmissionBatch) -> None:
    results_file = ResultsFile.objects.create(submission_batch=batch, content=b"x")

    assert results_file.public_code.startswith("F-")
    assert normalize(results_file.public_code, CodeType.RESULTS_FILE) == (
        results_file.public_code
    )
    assert str(results_file) == results_file.public_code


def test_defaults(batch: SubmissionBatch) -> None:
    results_file = ResultsFile.objects.create(submission_batch=batch)
    results_file.refresh_from_db()

    assert results_file.status == ResultsFileStatus.PENDING
    assert results_file.game_target == GameTarget.ELEVEN
    assert results_file.location == ""
    assert results_file.error_code == ""
    assert bytes(results_file.content) == b""
    assert results_file.report == []
    assert results_file.processed_at is None


def test_location_has_the_match_location_limit() -> None:
    assert ResultsFile._meta.get_field("location").max_length == (
        Match._meta.get_field("location").max_length
    )


def test_an_error_code_needs_the_failed_status(batch: SubmissionBatch) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ResultsFile.objects.create(
            submission_batch=batch,
            status=ResultsFileStatus.DONE,
            error_code="malformed",
        )


def test_a_failed_file_may_have_an_error_code(batch: SubmissionBatch) -> None:
    results_file = ResultsFile.objects.create(
        submission_batch=batch,
        status=ResultsFileStatus.FAILED,
        error_code="malformed",
    )

    assert results_file.error_code == "malformed"


def test_an_unknown_status_is_refused(batch: SubmissionBatch) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ResultsFile.objects.create(submission_batch=batch, status="lost")


def test_an_unknown_game_target_is_refused(batch: SubmissionBatch) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        ResultsFile.objects.create(submission_batch=batch, game_target=12)


def test_one_results_file_per_batch(batch: SubmissionBatch) -> None:
    ResultsFile.objects.create(submission_batch=batch)

    with pytest.raises(IntegrityError), transaction.atomic():
        ResultsFile.objects.create(submission_batch=batch)
