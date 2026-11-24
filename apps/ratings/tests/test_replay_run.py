"""The engine adapter's full replay (#47). Made-up data only.

``run_replay`` replays the given ledger through the engine and returns
the replay's provenance, a summary for every player and format the engine
holds, and the engine's snapshot, taken right after the replay.
"""

from datetime import date
from typing import Any

import open_engine
import pytest

from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, Team
from apps.players.models import GenderDivision, Player
from apps.ratings.engine_adapter import (
    ReplayRun,
    counted_engine_matches,
    live_config,
    load_config,
    rating_summary,
    replay_provenance,
    run_replay,
)
from apps.ratings.tests.builders import MIXED, saved_match

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 4, 30)
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _tournament(**kwargs: Any) -> tuple[Match, dict[Team, list[Player]]]:
    return saved_match(event=EventFactory.create(kind=EventKind.TOURNAMENT), **kwargs)


def _ledger() -> list[open_engine.Match]:
    _tournament(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        played_on=date(2026, 3, 1),
    )
    _tournament(divisions=MIXED, played_on=date(2026, 3, 8))
    return counted_engine_matches()


def test_a_replay_run_matches_a_direct_replay() -> None:
    ledger = _ledger()

    run = run_replay(ledger, AS_OF)

    engine = open_engine.replay(ledger, cfg=load_config(), as_of=AS_OF)
    assert engine.replay_info is not None
    assert run.provenance == replay_provenance(engine.replay_info)
    assert run.snapshot == engine.to_snapshot()
    expected = sorted(
        (
            rating_summary(engine.player_summary(pid, fmt))
            for pid, player in engine.players.items()
            for fmt in player.ratings
        ),
        key=lambda s: (s.rating_format, s.player_id),
    )
    assert list(run.summaries) == expected


def test_a_replay_run_covers_every_player_and_format() -> None:
    run = run_replay(_ledger(), AS_OF)

    pairs = [(s.rating_format, s.player_id) for s in run.summaries]
    assert pairs == sorted(pairs)
    formats = {s.rating_format for s in run.summaries}
    assert formats == {"singles", "doubles", "mixed_doubles"}
    # Two singles players, four doubles players in two doubles formats.
    assert len(run.summaries) == 2 + 4 * 2
    for summary in run.summaries:
        assert summary.calculated_as_of == AS_OF
        assert summary.live_updates_since_recalculation == 0


def test_the_snapshot_is_resumable() -> None:
    run = run_replay(_ledger(), AS_OF)

    assert run.snapshot["resumable"] is True
    assert run.snapshot["calculated_as_of"] == AS_OF.isoformat()


def test_a_replay_run_carries_the_loaded_config_labels() -> None:
    run = run_replay(_ledger(), AS_OF)

    labels = live_config()
    assert isinstance(run, ReplayRun)
    assert run.parameter_fingerprint == labels.parameter_fingerprint
    assert run.config_fingerprint == labels.config_fingerprint
    assert run.config_version == labels.config_version
    assert run.params_version == labels.params_version


def test_an_empty_ledger_gives_no_summaries() -> None:
    run = run_replay([], AS_OF)

    assert run.summaries == ()
    assert run.provenance.as_of == AS_OF
    assert run.provenance.matches_considered == 0
