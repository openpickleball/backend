"""The ``possible_duplicate`` warning follows what counts (#71, review
finding E).

When a run finishes, every recorded row whose match is confirmed but held
from counting gets the warning, so both rows of a held pair get it, and
a row whose hold was lifted (the other match voided) does not. Made-up
data only.
"""

import csv
import io
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import results_files, tasks
from apps.matches.csv_format import COLUMNS
from apps.matches.models import GameTarget, Match, ResultsFile
from apps.matches.results_files import create_results_file, process_results_file
from apps.matches.workflow import void_match
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _warnings(row: dict[str, Any]) -> list[str]:
    return [warning["code"] for warning in row["warnings"]]


def _pair_file() -> ResultsFile:
    """Two rows, same players and date, other scores: a held pair."""
    event: Event = EventFactory.create(organizer=verified_organizer())
    member: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=member)
    a: Player = PlayerFactory.create()
    b: Player = PlayerFactory.create()
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    for loser in ("7", "5"):
        writer.writerow(
            ["S", "SIDEOUT", "", "2026-06-01"]
            + [a.display_name, a.public_code, "", ""]
            + [b.display_name, b.public_code, "", ""]
            + ["11", loser]
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


def test_both_rows_of_a_held_pair_get_the_warning() -> None:
    results_file = _pair_file()

    process_results_file(results_file.pk)

    first, second = ResultsFile.objects.get(pk=results_file.pk).report
    assert first["result"] == second["result"] == "recorded"
    assert _warnings(first) == ["possible_duplicate"]
    assert _warnings(second) == ["possible_duplicate"]


def test_a_row_whose_hold_was_lifted_gets_no_warning() -> None:
    results_file = _pair_file()
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    real_record_match = results_files.record_match
    recorded: list[Match] = []

    def record_then_void_first(**values: Any) -> Any:
        result = real_record_match(**values)
        recorded.append(result.match)
        if len(recorded) == 2:
            void_match(recorded[0], actor=moderator, reason="Made-up reason.")
        return result

    with mock.patch.object(results_files, "record_match", record_then_void_first):
        process_results_file(results_file.pk)

    first, second = ResultsFile.objects.get(pk=results_file.pk).report
    assert _warnings(first) == []
    assert _warnings(second) == []
