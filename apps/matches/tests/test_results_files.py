"""Processing an uploaded CSV results file (#71): ``process_results_file``.

Each good row is recorded with ``record_match()`` as the uploader, in
its own transaction, linked to the upload's batch. Each row gets one
result in the report, which never holds a name or a submitted value.
Made-up data only.
"""

import csv
import io
import json
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import Permission
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import OrganizerMember
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import results_files, tasks
from apps.matches.csv_format import COLUMNS
from apps.matches.models import (
    GameTarget,
    Match,
    MatchFormat,
    MatchStatus,
    ResultsFile,
    ResultsFileStatus,
)
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.results_files import (
    create_results_file,
    process_results_file,
    sweep_results_files,
)
from apps.matches.services import record_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from config.celery import app as celery_app

pytestmark = pytest.mark.django_db

DATE = "2026-06-01"
LOCATION = "Made-up Park"


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
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(6)]


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    # Processing is called directly in these tests.
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _row(
    a: Sequence[Player | str],
    b: Sequence[Player | str],
    games: Sequence[tuple[int | str, int | str]],
    *,
    match_type: str = "S",
    label: str = "Made-up Round",
    names: dict[str, str] | None = None,
) -> list[str]:
    """One CSV row. A player is a Player (its code and display name) or a
    raw ID string (no name)."""
    names = names or {}
    cells = [match_type, "SIDEOUT", label, DATE]
    for team, slot_players in (("A", a), ("B", b)):
        for slot in (1, 2):
            player = slot_players[slot - 1] if len(slot_players) >= slot else None
            label_key = f"player{team}{slot}"
            if isinstance(player, Player):
                cells += [names.get(label_key, player.display_name), player.public_code]
            elif isinstance(player, str):
                cells += [names.get(label_key, ""), player]
            else:
                cells += ["", ""]
    for number in range(5):
        if number < len(games):
            cells += [str(games[number][0]), str(games[number][1])]
        else:
            cells += ["", ""]
    return cells


def _csv(*rows: list[str]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        if row:
            writer.writerow(row)
        else:
            out.write("\n")
    return out.getvalue().encode()


def _upload(member: User, event: Event, content: bytes) -> ResultsFile:
    return create_results_file(
        account=member,
        event=event,
        content=content,
        game_target=GameTarget.ELEVEN,
        location=LOCATION,
        players_13_or_older=True,
    )


def _processed(member: User, event: Event, content: bytes) -> ResultsFile:
    results_file = _upload(member, event, content)
    process_results_file(results_file.pk)
    results_file.refresh_from_db()
    return results_file


def _codes(items: list[dict[str, Any]]) -> list[tuple[list[str], str]]:
    return [(item["columns"], item["code"]) for item in items]


# AC1, AC4: good rows recorded, a bad row reported


def test_good_rows_are_recorded_and_a_bad_row_is_reported(
    event: Event, member: User, players: list[Player]
) -> None:
    content = _csv(
        _row(players[:1], players[1:2], [(11, 7)]),
        _row(players[2:3], players[3:4], [(11, 11)]),
        [],
        _row(players[4:5], players[5:6], [(11, 9), (11, 4)], label="Made-up Final"),
    )

    results_file = _processed(member, event, content)

    assert results_file.status == ResultsFileStatus.DONE
    assert results_file.error_code == ""
    assert results_file.processed_at is not None
    first, bad, last = results_file.report
    assert (first["row"], first["sequence"], first["result"]) == (2, 1, "recorded")
    assert (bad["row"], bad["sequence"], bad["result"]) == (3, 2, "failed")
    assert bad["match"] is None
    assert _codes(bad["issues"]) == [(["teamAGame1", "teamBGame1"], "tie")]
    assert (last["row"], last["sequence"], last["result"]) == (5, 3, "recorded")

    batch = results_file.submission_batch
    recorded = {match.public_code: match for match in batch.matches.all()}
    assert set(recorded) == {first["match"], last["match"]}
    match = recorded[last["match"]]
    assert match.status == MatchStatus.CONFIRMED
    assert match.submitted_by_user_id == member.pk
    assert match.event_id == event.pk
    assert match.location == LOCATION
    assert match.event_label == "Made-up Final"
    assert match.sequence == 3
    assert recorded[first["match"]].sequence == 1


def test_a_row_result_has_its_full_shape(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    (row,) = results_file.report
    assert row == {
        "row": 2,
        "sequence": 1,
        "result": "recorded",
        "match": row["match"],
        "issues": [],
        "warnings": [],
    }
    assert row["match"].startswith("M-")


# AC2: unknown players


def test_an_unknown_well_formed_id_fails_the_row_on_its_column(
    event: Event, member: User, players: list[Player]
) -> None:
    unknown = generate(CodeType.PLAYER)

    results_file = _processed(
        member, event, _csv(_row(players[:1], [unknown], [(11, 7)]))
    )

    (row,) = results_file.report
    assert row["result"] == "failed"
    assert _codes(row["issues"]) == [(["playerB1Id"], "unknown_player")]
    assert unknown not in json.dumps(results_file.report)
    assert Match.objects.count() == 0


def test_a_player_whose_account_is_inactive_is_unknown(
    event: Event, member: User, players: list[Player]
) -> None:
    inactive = players[1]
    inactive.account.is_active = False
    inactive.account.save(update_fields=["is_active"])

    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    (row,) = results_file.report
    assert _codes(row["issues"]) == [(["playerB1Id"], "unknown_player")]
    assert Match.objects.count() == 0


def test_a_malformed_id_is_unknown_too(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member, event, _csv(_row(players[:1], ["not-an-id"], [(11, 7)]))
    )

    (row,) = results_file.report
    assert _codes(row["issues"]) == [(["playerB1Id"], "unknown_player")]
    assert "not-an-id" not in json.dumps(results_file.report)


# AC3: the same file again


def test_uploading_the_same_file_again_records_nothing_new(
    event: Event, member: User, players: list[Player]
) -> None:
    content = _csv(
        _row(players[:1], players[1:2], [(11, 7)]),
        _row(players[2:3], players[3:4], [(11, 5)]),
    )
    first = _processed(member, event, content)
    count = Match.objects.count()

    second = _processed(member, event, content)

    assert Match.objects.count() == count
    assert [row["result"] for row in second.report] == ["duplicate", "duplicate"]
    assert [row["match"] for row in second.report] == [
        row["match"] for row in first.report
    ]
    assert _codes(second.report[0]["issues"]) == [([], "duplicate_match")]
    assert second.submission_batch.matches.count() == 0


def test_a_row_repeating_an_earlier_row_of_the_file_is_a_duplicate(
    event: Event, member: User, players: list[Player]
) -> None:
    row = _row(players[:1], players[1:2], [(11, 7)])

    results_file = _processed(member, event, _csv(row, row))

    first, second = results_file.report
    assert first["result"] == "recorded"
    assert second["result"] == "duplicate"
    assert second["match"] == first["match"]


def test_a_pending_match_is_never_confirmed_by_an_upload(
    event: Event, member: User, players: list[Player]
) -> None:
    pending = record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:2],
        games=((11, 7),),
        played_on=date.fromisoformat(DATE),
        submitted_by_user=players[0].account,
    ).match

    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    (row,) = results_file.report
    assert row["result"] == "duplicate"
    # The organizer neither submitted it nor played in it.
    assert row["match"] is None
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING


# AC16: safe to run twice


def test_a_second_run_reports_the_rows_the_first_run_recorded(
    event: Event, member: User, players: list[Player]
) -> None:
    content = _csv(
        _row(players[:1], players[1:2], [(11, 7)]),
        _row(players[2:3], players[3:4], [(11, 5)]),
    )
    results_file = _upload(member, event, content)
    # A first run that stopped before storing its report.
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)
    codes = set(
        results_file.submission_batch.matches.values_list("public_code", flat=True)
    )
    assert len(codes) == 2

    process_results_file(results_file.pk)

    results_file.refresh_from_db()
    assert [row["result"] for row in results_file.report] == ["recorded", "recorded"]
    assert {row["match"] for row in results_file.report} == codes
    assert Match.objects.count() == 2


def test_a_file_no_longer_pending_is_skipped(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )
    report = results_file.report

    process_results_file(results_file.pk)

    results_file.refresh_from_db()
    assert results_file.report == report
    assert Match.objects.count() == 1


def test_a_missing_file_is_skipped() -> None:
    process_results_file(987654321)


# AC6: too many rows


def test_a_file_over_the_row_limit_fails_as_a_whole(
    event: Event, member: User, players: list[Player]
) -> None:
    rows = [_row(players[:1], players[1:2], [(11, n)]) for n in range(3)]

    with mock.patch.object(results_files, "MAX_ROWS", 2):
        results_file = _processed(member, event, _csv(*rows))

    assert results_file.status == ResultsFileStatus.FAILED
    assert results_file.error_code == "too_many_rows"
    assert results_file.report == []
    assert bytes(results_file.content) == b""
    assert Match.objects.count() == 0


def test_a_file_at_the_row_limit_is_processed(
    event: Event, member: User, players: list[Player]
) -> None:
    rows = [_row(players[:1], players[1:2], [(11, n)]) for n in range(2)]

    with mock.patch.object(results_files, "MAX_ROWS", 2):
        results_file = _processed(member, event, _csv(*rows))

    assert results_file.status == ResultsFileStatus.DONE
    assert len(results_file.report) == 2


# File errors and AC14


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (b"\xff\xfe not utf-8", "not_utf8"),
        (b'"never closed', "malformed"),
        (b"a,b,c\n", "bad_header"),
        (_csv(), "no_rows"),
    ],
)
def test_a_file_the_parser_cannot_read_fails_with_its_code(
    event: Event, member: User, content: bytes, code: str
) -> None:
    results_file = _processed(member, event, content)

    assert results_file.status == ResultsFileStatus.FAILED
    assert results_file.error_code == code
    assert results_file.report == []
    assert bytes(results_file.content) == b""
    assert results_file.processed_at is not None


def test_the_file_is_emptied_once_processed(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    assert bytes(results_file.content) == b""


# AC12: name warnings


def test_a_name_that_differs_gives_a_warning_and_the_row_records(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member,
        event,
        _csv(
            _row(
                players[:1],
                players[1:2],
                [(11, 7)],
                names={"playerB1": "Made-up Othername"},
            )
        ),
    )

    (row,) = results_file.report
    assert row["result"] == "recorded"
    assert _codes(row["warnings"]) == [(["playerB1"], "name_mismatch")]
    assert "Made-up Othername" not in json.dumps(results_file.report)


def test_case_and_spacing_do_not_make_a_name_differ(
    event: Event, member: User, players: list[Player]
) -> None:
    name = players[0].display_name
    spaced = "  " + "  ".join(name.upper().split()) + " "

    results_file = _processed(
        member,
        event,
        _csv(_row(players[:1], players[1:2], [(11, 7)], names={"playerA1": spaced})),
    )

    (row,) = results_file.report
    assert row["warnings"] == []


def test_a_blank_name_gives_no_warning(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member,
        event,
        _csv(_row(players[:1], players[1:2], [(11, 7)], names={"playerA1": ""})),
    )

    (row,) = results_file.report
    assert row["result"] == "recorded"
    assert row["warnings"] == []


# AC15: no names or IDs in the report


def test_the_report_holds_no_name_or_id_from_the_file(
    event: Event, member: User, players: list[Player]
) -> None:
    unknown = generate(CodeType.PLAYER)
    content = _csv(
        _row(players[:1], players[1:2], [(11, 7)], names={"playerA1": "Made-up X"}),
        _row(players[2:3], [unknown], [(11, 7)]),
        _row(players[4:5], players[5:6], [(11, 11)]),
    )

    results_file = _processed(member, event, content)

    text = json.dumps(results_file.report)
    for player in players:
        assert player.display_name not in text
        assert player.public_code not in text
    assert unknown not in text
    assert "Made-up X" not in text
    assert "Made-up Round" not in text
    assert LOCATION not in text


# record_match refusals


def test_a_score_above_the_model_limit_fails_on_its_game(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(40000, 39998)]))
    )

    (row,) = results_file.report
    assert row["result"] == "failed"
    assert {tuple(columns) for columns, _code in _codes(row["issues"])} == {
        ("teamAGame1", "teamBGame1")
    }
    assert Match.objects.count() == 0


def test_a_membership_removed_before_processing_fails_every_row(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _upload(
        member,
        event,
        _csv(
            _row(players[:1], players[1:2], [(11, 7)]),
            _row(players[2:3], players[3:4], [(11, 5)]),
        ),
    )
    OrganizerMember.objects.filter(account=member).delete()

    process_results_file(results_file.pk)

    results_file.refresh_from_db()
    assert [row["result"] for row in results_file.report] == ["failed", "failed"]
    assert _codes(results_file.report[0]["issues"]) == [([], "forbidden")]
    assert Match.objects.count() == 0


def test_a_near_duplicate_records_with_a_warning(
    event: Event, member: User, players: list[Player]
) -> None:
    earlier = _processed(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    later = _processed(member, event, _csv(_row(players[:1], players[1:2], [(11, 5)])))

    (row,) = later.report
    assert row["result"] == "recorded"
    assert row["match"] != earlier.report[0]["match"]
    assert _codes(row["warnings"]) == [([], "possible_duplicate")]


def test_a_doubles_row_records(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _processed(
        member,
        event,
        _csv(_row(players[:2], players[2:4], [(11, 7)], match_type="D")),
    )

    (row,) = results_file.report
    assert row["result"] == "recorded"
    match = Match.objects.get(public_code=row["match"])
    assert match.format == MatchFormat.DOUBLES


# The tasks and the sweep (AC5)


def test_the_processing_task_routes_to_the_default_queue() -> None:
    assert tasks.process_results_file.name == "apps.matches.tasks.process_results_file"
    route = celery_app.amqp.router.route({}, tasks.process_results_file.name)
    assert route["queue"].name == "default"


def test_the_processing_task_limits_stay_under_the_visibility_timeout() -> None:
    assert tasks.process_results_file.soft_time_limit == 1500
    assert tasks.process_results_file.time_limit == 1800
    assert timedelta(seconds=1800) < results_files.REQUEUE_AFTER


def test_the_sweep_task_routes_to_the_default_queue() -> None:
    assert tasks.sweep_results_files.name == "apps.matches.tasks.sweep_results_files"
    route = celery_app.amqp.router.route({}, tasks.sweep_results_files.name)
    assert route["queue"].name == "default"


def test_the_sweep_runs_every_fifteen_minutes() -> None:
    from django.conf import settings

    entry = settings.CELERY_BEAT_SCHEDULE["sweep-results-files"]
    assert entry["task"] == "apps.matches.tasks.sweep_results_files"
    assert entry["options"] == {"expire_seconds": 900}


def test_the_task_processes_the_file(
    event: Event, member: User, players: list[Player]
) -> None:
    results_file = _upload(
        member, event, _csv(_row(players[:1], players[1:2], [(11, 7)]))
    )

    tasks.process_results_file.apply(args=(results_file.pk,)).get()

    results_file.refresh_from_db()
    assert results_file.status == ResultsFileStatus.DONE


def test_the_sweep_fails_a_file_pending_too_long(
    event: Event, member: User, no_queue: Any
) -> None:
    results_file = _upload(member, event, b"made-up content")
    ResultsFile.objects.filter(pk=results_file.pk).update(
        created_at=timezone.now() - results_files.PENDING_EXPIRY - timedelta(minutes=1)
    )

    sweep_results_files()

    results_file.refresh_from_db()
    assert results_file.status == ResultsFileStatus.FAILED
    assert results_file.error_code == "expired"
    assert bytes(results_file.content) == b""
    no_queue.assert_not_called()


def test_the_sweep_queues_a_lost_file_again_once(
    event: Event,
    member: User,
    no_queue: Any,
    django_capture_on_commit_callbacks: Any,
) -> None:
    results_file = _upload(member, event, b"made-up content")
    fresh = _upload(member, event, b"made-up content")
    ResultsFile.objects.filter(pk=results_file.pk).update(
        queued_at=timezone.now() - results_files.REQUEUE_AFTER - timedelta(minutes=1)
    )

    with django_capture_on_commit_callbacks(execute=True):
        assert sweep_results_files() == 1
    with django_capture_on_commit_callbacks(execute=True):
        assert sweep_results_files() == 0

    no_queue.assert_called_once_with(results_file.pk)
    fresh.refresh_from_db()
    assert fresh.status == ResultsFileStatus.PENDING


def test_a_moderator_who_is_not_a_member_may_not_upload(
    event: Event, players: list[Player]
) -> None:
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    with pytest.raises(MatchPermissionDenied):
        _upload(moderator, event, b"made-up content")
