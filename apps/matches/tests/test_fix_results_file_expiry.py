"""The sweep expires only a file no run can still be processing, and keeps
its row results (#71, review finding B). Made-up data only."""

import csv
import io
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import results_files, tasks
from apps.matches.csv_format import COLUMNS
from apps.matches.models import GameTarget, ResultsFile, ResultsFileStatus
from apps.matches.results_files import (
    PENDING_EXPIRY,
    REQUEUE_AFTER,
    create_results_file,
    process_results_file,
    sweep_results_files,
)
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


@pytest.fixture
def results_file() -> ResultsFile:
    event: Event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    a, b = PlayerFactory.create(), PlayerFactory.create()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerow(
        ["S", "SIDEOUT", "", "2026-06-01"]
        + [a.display_name, a.public_code, "", ""]
        + [b.display_name, b.public_code, "", ""]
        + ["11", "7"]
        + [""] * 8
    )
    return create_results_file(
        account=member,
        event=event,
        content=out.getvalue().encode(),
        game_target=GameTarget.ELEVEN,
        location="",
        players_13_or_older=True,
    )


def _age(results_file: ResultsFile, *, queued_ago: timedelta) -> None:
    now = timezone.now()
    ResultsFile.objects.filter(pk=results_file.pk).update(
        created_at=now - PENDING_EXPIRY - timedelta(minutes=1),
        queued_at=now - queued_ago,
    )


def test_a_file_queued_recently_is_not_expired(
    results_file: ResultsFile, no_queue: Any
) -> None:
    _age(results_file, queued_ago=timedelta(minutes=1))

    sweep_results_files()

    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.PENDING
    assert bytes(stored.content) != b""
    no_queue.assert_not_called()


def test_an_expired_file_keeps_its_row_results(results_file: ResultsFile) -> None:
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)
    report = ResultsFile.objects.get(pk=results_file.pk).report
    assert [row["result"] for row in report] == ["recorded"]
    _age(results_file, queued_ago=REQUEUE_AFTER + timedelta(minutes=1))

    sweep_results_files()

    stored = ResultsFile.objects.get(pk=results_file.pk)
    assert stored.status == ResultsFileStatus.FAILED
    assert stored.error_code == "expired"
    assert bytes(stored.content) == b""
    assert stored.report == report
