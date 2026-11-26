"""A bulk void of an upload's batch stays done (#71, review finding A).

A run that stopped partway leaves the file pending. A moderator who then
voids the batch fails the file, so no later run records its rows again.
A run in flight stops at the next row once the batch is voided. A row
whose match this batch already holds, in any status, is never recorded
again: a voided one is reported as ``voided``, any other as recorded,
even after the uploader lost membership or a player's Account was
deactivated. Row results are stored as each row is processed, so a
stopped run keeps them. Made-up data only.
"""

import csv
import io
import re
from collections.abc import Sequence
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerMember
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import results_files, tasks, workflow
from apps.matches.csv_format import COLUMNS
from apps.matches.models import (
    GameTarget,
    Match,
    MatchStatus,
    ResultsFile,
    ResultsFileStatus,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.results_files import create_results_file, process_results_file
from apps.matches.workflow import void_match, void_submission_batch
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


@pytest.fixture
def moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


def _row(a: Player, b: Player, games: Sequence[tuple[int, int]]) -> list[str]:
    cells = ["S", "SIDEOUT", "Made-up Round", "2026-06-01"]
    cells += [a.display_name, a.public_code, "", ""]
    cells += [b.display_name, b.public_code, "", ""]
    for number in range(5):
        cells += [str(s) for s in games[number]] if number < len(games) else ["", ""]
    return cells


def _file(member: User, event: Event, players: list[Player]) -> ResultsFile:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerow(_row(players[0], players[1], [(11, 7)]))
    writer.writerow(_row(players[2], players[3], [(11, 5)]))
    return create_results_file(
        account=member,
        event=event,
        content=out.getvalue().encode(),
        game_target=GameTarget.ELEVEN,
        location="",
        players_13_or_older=True,
    )


def _stopped_run(results_file: ResultsFile) -> None:
    """A run that recorded every row and stopped before it finished."""
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)


def _refreshed(results_file: ResultsFile) -> ResultsFile:
    return ResultsFile.objects.get(pk=results_file.pk)


def test_a_stopped_run_keeps_its_row_results(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)

    _stopped_run(results_file)

    stored = _refreshed(results_file)
    assert stored.status == ResultsFileStatus.PENDING
    assert [row["sequence"] for row in stored.report] == [1, 2]
    assert [row["result"] for row in stored.report] == ["recorded", "recorded"]


def test_a_bulk_void_after_a_stopped_run_fails_the_file(
    event: Event, member: User, moderator: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    _stopped_run(results_file)
    batch = results_file.submission_batch

    void_submission_batch(batch, actor=moderator, reason=REASON)

    stored = _refreshed(results_file)
    assert stored.status == ResultsFileStatus.FAILED
    assert stored.error_code == "voided"
    assert bytes(stored.content) == b""
    assert [row["result"] for row in stored.report] == ["recorded", "recorded"]


def test_no_run_after_a_bulk_void_records_the_rows_again(
    event: Event, member: User, moderator: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    _stopped_run(results_file)
    batch = results_file.submission_batch
    void_submission_batch(batch, actor=moderator, reason=REASON)
    count = Match.objects.count()

    process_results_file(results_file.pk)

    assert Match.objects.count() == count
    assert not batch.matches.exclude(status=MatchStatus.VOIDED).exists()


def test_a_run_in_flight_stops_once_the_batch_is_voided(
    event: Event, member: User, moderator: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    batch = results_file.submission_batch
    real_record_match = results_files.record_match
    calls: list[int] = []

    def record_then_void(**values: Any) -> Any:
        recorded = real_record_match(**values)
        calls.append(values["sequence"])
        if len(calls) == 1:
            void_submission_batch(batch, actor=moderator, reason=REASON)
        return recorded

    with mock.patch.object(results_files, "record_match", record_then_void):
        process_results_file(results_file.pk)

    assert calls == [1]
    assert batch.matches.count() == 1
    assert not batch.matches.exclude(status=MatchStatus.VOIDED).exists()
    stored = _refreshed(results_file)
    assert stored.status == ResultsFileStatus.FAILED
    assert stored.error_code == "voided"


def test_a_row_whose_match_was_voided_is_reported_voided(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    _stopped_run(results_file)
    first, second = results_file.submission_batch.matches.order_by("sequence")
    void_match(first, actor=member)
    count = Match.objects.count()

    process_results_file(results_file.pk)

    stored = _refreshed(results_file)
    assert Match.objects.count() == count
    voided, recorded = stored.report
    assert voided["result"] == "failed"
    assert [(i["columns"], i["code"]) for i in voided["issues"]] == [([], "voided")]
    assert recorded["result"] == "recorded"
    assert recorded["match"] == second.public_code


def test_a_rerun_after_membership_loss_reports_recorded_rows(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    _stopped_run(results_file)
    codes = set(
        results_file.submission_batch.matches.values_list("public_code", flat=True)
    )
    OrganizerMember.objects.filter(account=member).delete()

    process_results_file(results_file.pk)

    stored = _refreshed(results_file)
    assert [row["result"] for row in stored.report] == ["recorded", "recorded"]
    assert {row["match"] for row in stored.report} == codes


def test_a_rerun_after_a_player_was_deactivated_reports_recorded_rows(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    _stopped_run(results_file)
    account = players[1].account
    account.is_active = False
    account.save(update_fields=["is_active"])

    process_results_file(results_file.pk)

    stored = _refreshed(results_file)
    assert [row["result"] for row in stored.report] == ["recorded", "recorded"]


def test_a_bulk_void_leaves_a_done_file_done(
    event: Event, member: User, moderator: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)
    process_results_file(results_file.pk)
    report = _refreshed(results_file).report

    void_submission_batch(results_file.submission_batch, actor=moderator, reason=REASON)

    stored = _refreshed(results_file)
    assert stored.status == ResultsFileStatus.DONE
    assert stored.report == report


def test_a_refused_bulk_void_leaves_the_file_pending(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)

    with pytest.raises(MatchActionDenied):
        void_submission_batch(
            results_file.submission_batch, actor=member, reason=REASON
        )

    stored = _refreshed(results_file)
    assert stored.status == ResultsFileStatus.PENDING
    assert bytes(stored.content) != b""


def test_a_blank_reason_leaves_the_file_pending(
    event: Event, member: User, moderator: User, players: list[Player]
) -> None:
    results_file = _file(member, event, players)

    with pytest.raises(ValueError, match=re.escape(workflow._BULK_REASON)):
        void_submission_batch(results_file.submission_batch, actor=moderator, reason="")

    assert _refreshed(results_file).status == ResultsFileStatus.PENDING
