"""A row whose batch match was voided says so in neutral words (#71).

The match may have been voided outright, by an Organizer's correction,
or by a moderator's bulk void, so the issue never says who voided it,
and the row keeps the voided match's OPEN Code. Made-up data only.
"""

from datetime import date
from typing import Any
from unittest import mock

import pytest

from apps.matches import results_files, tasks
from apps.matches.models import MatchFormat, ResultsFile
from apps.matches.results_files import process_results_file
from apps.matches.tests.results_file_builders import (
    organizer_member,
    row,
    upload,
)
from apps.matches.workflow import edit_match, void_match
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

VOIDED_TEXT = "This row's match was voided."


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


@pytest.mark.parametrize("by", ["void", "correction"])
def test_a_voided_rows_issue_is_neutral_and_keeps_the_code(by: str) -> None:
    event, member = organizer_member()
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 7)])
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)
    (match,) = results_file.submission_batch.matches.all()
    if by == "void":
        void_match(match, actor=member)
    else:
        edit_match(
            match,
            actor=member,
            format=MatchFormat.SINGLES,
            team_a=[a],
            team_b=[b],
            games=((11, 3),),
            played_on=date(2026, 6, 1),
        )

    process_results_file(results_file.pk)

    (result,) = ResultsFile.objects.get(pk=results_file.pk).report
    assert result["result"] == "failed"
    assert result["match"] == match.public_code
    assert result["issues"] == [
        {"columns": [], "code": "voided", "message": VOIDED_TEXT}
    ]
