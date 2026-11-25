"""Storing an uploaded CSV results file (#71): ``create_results_file``.

One Submission Batch and one Results File per upload, created in one
transaction after the Event row is share-locked, with one Audit Event,
and the processing task queued after commit. Made-up data only.
"""

import logging
from typing import Any
from unittest import mock

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from kombu.exceptions import OperationalError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import tasks
from apps.matches.models import (
    GameTarget,
    ResultsFile,
    ResultsFileStatus,
    SubmissionBatch,
)
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.results_files import MAX_FILE_BYTES, create_results_file

pytestmark = pytest.mark.django_db

CONTENT = b"made-up file content"


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _create(account: User, event: Event, **overrides: Any) -> ResultsFile:
    values: dict[str, Any] = {
        "account": account,
        "event": event,
        "content": CONTENT,
        "game_target": GameTarget.ELEVEN,
        "location": "",
        "players_13_or_older": True,
    }
    values.update(overrides)
    return create_results_file(**values)


def _counts() -> tuple[int, int, int]:
    return (
        ResultsFile.objects.count(),
        SubmissionBatch.objects.count(),
        AuditEvent.objects.count(),
    )


@pytest.fixture
def delay() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def test_it_stores_the_file_with_its_batch(
    event: Event, member: User, delay: Any
) -> None:
    results_file = _create(
        member, event, game_target=GameTarget.FIFTEEN, location=" Made-up Park "
    )

    stored = ResultsFile.objects.select_related("submission_batch").get(
        pk=results_file.pk
    )
    batch = stored.submission_batch
    assert batch.submitted_by_user_id == member.pk
    assert batch.submitted_by_application_id is None
    assert batch.event_id == event.pk
    assert batch.club_id is None
    assert stored.status == ResultsFileStatus.PENDING
    assert bytes(stored.content) == CONTENT
    assert stored.game_target == GameTarget.FIFTEEN
    assert stored.location == "Made-up Park"
    assert stored.report == []


def test_it_records_one_audit_event_without_file_content(
    event: Event, member: User, delay: Any
) -> None:
    results_file = _create(member, event, location="Made-up Park")

    events = list(AuditEvent.objects.filter(action=AuditAction.RESULTS_FILE_CREATE))
    assert len(events) == 1
    audit = events[0]
    assert audit.actor_user_id == member.pk
    assert audit.target_object_id == str(results_file.pk)
    assert audit.before is None
    assert audit.after == {
        "event": event.public_code,
        "game_target": 11,
        "players_13_or_older": True,
    }


def test_it_queues_processing_after_commit(
    event: Event,
    member: User,
    delay: Any,
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        results_file = _create(member, event)
    delay.assert_not_called()

    for callback in callbacks:
        callback()

    delay.assert_called_once_with(results_file.pk)


def test_a_broker_error_is_logged_by_id_only(
    event: Event,
    member: User,
    delay: Any,
    django_capture_on_commit_callbacks: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    delay.side_effect = OperationalError("made-up broker text")
    with (
        caplog.at_level(logging.ERROR),
        django_capture_on_commit_callbacks(execute=True),
    ):
        results_file = _create(member, event)

    assert str(results_file.pk) in caplog.text
    assert "OperationalError" in caplog.text
    assert "made-up broker text" not in caplog.text
    assert ResultsFile.objects.filter(pk=results_file.pk).exists()


def test_it_share_locks_the_event_before_the_batch(
    event: Event, member: User, delay: Any
) -> None:
    with CaptureQueriesContext(connection) as queries:
        _create(member, event)

    sql = [query["sql"] for query in queries.captured_queries]
    lock = next(
        index
        for index, text in enumerate(sql)
        if "FOR KEY SHARE" in text and '"events_event"' in text
    )
    insert = next(
        index
        for index, text in enumerate(sql)
        if text.startswith('INSERT INTO "matches_submissionbatch"')
    )
    assert lock < insert


def test_without_the_age_confirmation_nothing_is_written(
    event: Event, member: User, delay: Any
) -> None:
    before = _counts()

    with pytest.raises(ValueError, match="13 or older"):
        _create(member, event, players_13_or_older=False)

    assert _counts() == before
    delay.assert_not_called()


def test_an_oversized_file_is_refused(event: Event, member: User, delay: Any) -> None:
    before = _counts()

    with pytest.raises(ValueError, match="too large"):
        _create(member, event, content=b"x" * (MAX_FILE_BYTES + 1))

    assert _counts() == before


def test_a_file_of_the_size_limit_is_accepted(
    event: Event, member: User, delay: Any
) -> None:
    results_file = _create(member, event, content=b"x" * MAX_FILE_BYTES)

    assert len(bytes(results_file.content)) == MAX_FILE_BYTES


def test_a_non_member_is_refused(event: Event, delay: Any) -> None:
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _create(UserFactory.create(), event)

    assert _counts() == before
    delay.assert_not_called()


def test_a_member_of_an_unverified_organizer_is_refused(
    event: Event, member: User, delay: Any
) -> None:
    organizer: Organizer = event.organizer
    verifier = organizer.verified_by
    assert verifier is not None
    unverify_organizer(organizer, actor=verifier, reason="Made-up reason.")
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _create(member, event)

    assert _counts() == before
