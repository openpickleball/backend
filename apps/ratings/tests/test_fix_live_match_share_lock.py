"""The live update holds the Match row so it cannot change before commit (#46).

After its snapshot and Player locks, the live update locks the Match row
``FOR SHARE`` and checks ``counted()`` again. A void, dispute, or edit
(which lock the row ``FOR UPDATE``) then waits until the live update
commits, so a Match voided after the check is never rated live. A second
connection tries to take the workflow's lock while the update holds its
own. Runs in a real transaction, so it writes no Audit Event (the audit
table refuses the teardown's flush once it has a row). Made-up data
only.
"""

from typing import Any

import pytest
from django.db import OperationalError, connection

from apps.audit.models import AuditEvent
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.players.models import GenderDivision
from apps.ratings import services
from apps.ratings.services import update_ratings_for_match
from apps.ratings.tests.builders import saved_match

pytestmark = pytest.mark.django_db(transaction=True)

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


class _Stop(Exception):
    """Ends the live update after the probe, so nothing is written."""


def test_the_match_row_is_locked_while_the_update_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Self-reported and confirmed by both teams: no Event, no Audit Event.
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        confirm=[(Team.A, 0), (Team.B, 0)],
    )
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    probes: list[str] = []

    def probe(*args: Any, **kwargs: Any) -> Any:
        other = connection.copy()
        try:
            with other.cursor() as cursor:
                cursor.execute(
                    f"SELECT 1 FROM {Match._meta.db_table} "
                    "WHERE id = %s FOR UPDATE NOWAIT",
                    [match.pk],
                )
            probes.append("acquired")
        except OperationalError:
            probes.append("refused")
        finally:
            other.close()
        raise _Stop

    monkeypatch.setattr(services, "_record_and_apply", probe)

    with pytest.raises(Exception):  # noqa: B017, PT011 (the probe's stop)
        update_ratings_for_match(match.pk)

    assert probes == ["refused"]
    assert not AuditEvent.objects.exists()
