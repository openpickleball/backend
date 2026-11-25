"""A rerun keeps the name warnings of rows an earlier run recorded (#71).

A row whose match this batch already holds gets the same result as when
it was first recorded, its name warnings included. Made-up data only.
"""

from typing import Any
from unittest import mock

import pytest

from apps.matches import results_files, tasks
from apps.matches.models import ResultsFile
from apps.matches.results_files import process_results_file
from apps.matches.tests.results_file_builders import organizer_member, row, upload
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_queue() -> Any:
    with mock.patch.object(tasks.process_results_file, "delay") as patched:
        yield patched


def _warnings(result: dict[str, Any]) -> list[tuple[list[str], str]]:
    return [(item["columns"], item["code"]) for item in result["warnings"]]


@pytest.mark.parametrize("deactivated", [False, True], ids=["active", "deactivated"])
def test_a_rerun_keeps_a_recorded_rows_name_warning(deactivated: bool) -> None:
    event, member = organizer_member()
    a, b = PlayerFactory.create(), PlayerFactory.create()
    results_file = upload(member, event, [row(a, b, 7, name_a="Made-up Othername")])
    with mock.patch.object(results_files, "_finish"):
        process_results_file(results_file.pk)
    first = ResultsFile.objects.get(pk=results_file.pk).report
    assert _warnings(first[0]) == [(["playerA1"], "name_mismatch")]
    if deactivated:
        a.account.is_active = False
        a.account.save(update_fields=["is_active"])

    process_results_file(results_file.pk)

    (result,) = ResultsFile.objects.get(pk=results_file.pk).report
    assert result["result"] == "recorded"
    assert _warnings(result) == [(["playerA1"], "name_mismatch")]
