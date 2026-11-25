"""A match that fails both expiry rules counts as one failure (#268, review fix).

A disputed match past the dispute period that is also the newer match of
an expired hold was tried by both rules. When its void failed, the run
reported two failures for one match and logged it twice. Made-up data
only.
"""

import logging
from datetime import timedelta

import pytest
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.matches import workflow
from apps.matches.models import Match, MatchFlag, MatchFormat, MatchStatus
from apps.matches.services import record_match
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON
from apps.matches.workflow import (
    ExpiryFailed,
    confirm_match,
    dispute_match,
    expire_disputes_and_holds,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _default_periods(settings: Settings) -> None:
    settings.MATCH_DISPUTE_EXPIRY_DAYS = 30
    settings.MATCH_DUPLICATE_HOLD_DAYS = 14


def test_a_match_failing_both_rules_is_one_failure(
    confirmed: Match,
    players: list[Player],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    later = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(later, actor=players[0].account)
    dispute_match(later, actor=players[2].account)
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    assert Match.objects.get(pk=confirmed.pk).status == MatchStatus.CONFIRMED

    def fail(locked: Match, **kwargs: object) -> None:
        raise RuntimeError("made-up failure")

    monkeypatch.setattr(workflow, "_void", fail)

    with (
        caplog.at_level(logging.ERROR, logger="apps.matches.workflow"),
        pytest.raises(ExpiryFailed) as error,
    ):
        expire_disputes_and_holds(now=timezone.now() + timedelta(days=31))

    assert error.value.failed == 1
    failures = [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR and str(later.pk) in record.getMessage()
    ]
    assert len(failures) == 1
