"""The live rating update for one confirmed Match (#46, ADR 0027).

``update_ratings_for_match`` locks the engine snapshot row, then the
Match's Players by id, records the Match on the engine the snapshot holds,
stores each changed rating through ``apply_rating()``, and saves the new
snapshot, all in one transaction. A late match writes nothing and hands
the players to ``schedule_replay``. Made-up data only.
"""

import logging
from datetime import date, timedelta
from typing import Any

import open_engine
import pytest

from apps.audit.models import AuditAction, AuditEvent
from apps.events.models import Event, EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, Match, MatchFlag, MatchFormat, Team
from apps.matches.tests.factories import raw_update
from apps.players.models import GenderDivision, Player
from apps.ratings import services
from apps.ratings.engine_adapter import (
    live_config,
    load_config,
    record_live,
    to_engine_match,
)
from apps.ratings.models import (
    EngineSnapshot,
    RatingChangeKind,
    RatingHistory,
    RatingState,
)
from apps.ratings.services import (
    LiveUpdateOutcome,
    schedule_replay,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import MIXED, PLAYED_ON, saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _tournament() -> Event:
    event: Event = EventFactory.create(kind=EventKind.TOURNAMENT)
    return event


def _singles(**kwargs: Any) -> tuple[Match, dict[Team, list[Player]]]:
    kwargs.setdefault("divisions", MENS_SINGLES)
    return saved_match(match_format=MatchFormat.SINGLES, event=_tournament(), **kwargs)


def _players(players: dict[Team, list[Player]]) -> list[Player]:
    return [player for team in Team for player in players[team]]


def _rating_events() -> int:
    return AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count()


def _stored_snapshot() -> dict[str, object] | None:
    row = EngineSnapshot.objects.filter(
        parameter_fingerprint=live_config().parameter_fingerprint
    ).first()
    return None if row is None else row.snapshot


def test_a_confirmed_singles_match_updates_both_players() -> None:
    match, players = _singles()

    outcome = update_ratings_for_match(match.pk)

    assert outcome == LiveUpdateOutcome.UPDATED
    engine_result = open_engine.RatingEngine(load_config()).record(
        to_engine_match(match)
    )
    for player in _players(players):
        state = RatingState.objects.get(player=player)
        assert state.rating_format == "singles"
        assert state.matches_played == 1
        assert state.live_updates_since_recalculation == 1
        assert state.provisional
        history = RatingHistory.objects.get(player=player)
        assert history.kind == RatingChangeKind.LIVE_UPDATE
        assert history.match == match
        assert history.delta == engine_result.deltas[str(player.public_code)]
    assert _rating_events() == 2


def test_the_snapshot_row_is_keyed_by_the_parameter_fingerprint() -> None:
    match, _ = _singles()

    update_ratings_for_match(match.pk)

    row = EngineSnapshot.objects.get()
    identity = live_config()
    assert row.parameter_fingerprint == identity.parameter_fingerprint
    assert row.config_fingerprint == identity.config_fingerprint
    assert row.config_version == identity.config_version
    assert row.params_version == identity.params_version
    assert str(match.public_code) in row.snapshot["processed_match_ids"]


def test_a_category_doubles_match_updates_its_format_and_overall_doubles() -> None:
    match, players = saved_match(divisions=MIXED, event=_tournament())

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED

    for player in _players(players):
        formats = sorted(
            RatingState.objects.filter(player=player).values_list(
                "rating_format", flat=True
            )
        )
        assert formats == ["doubles", "mixed_doubles"]
        assert (
            RatingHistory.objects.filter(
                player=player, match=match, kind=RatingChangeKind.LIVE_UPDATE
            ).count()
            == 2
        )
    assert _rating_events() == 8


def test_running_twice_changes_nothing_the_second_time() -> None:
    match, _ = _singles()
    update_ratings_for_match(match.pk)
    history = RatingHistory.objects.count()
    events = _rating_events()
    snapshot = _stored_snapshot()

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UNCHANGED

    assert RatingHistory.objects.count() == history
    assert _rating_events() == events
    assert _stored_snapshot() == snapshot


def test_a_redelivery_after_a_later_match_is_unchanged_not_late(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, players = _singles()
    later, _ = _singles(players=players, played_on=PLAYED_ON + timedelta(days=2))
    update_ratings_for_match(first.pk)
    update_ratings_for_match(later.pk)
    calls: list[object] = []
    monkeypatch.setattr(
        services, "schedule_replay", lambda *args, **kwargs: calls.append(args)
    )

    assert update_ratings_for_match(first.pk) == LiveUpdateOutcome.UNCHANGED

    assert calls == []


def test_a_second_match_builds_on_the_first() -> None:
    first, players = _singles()
    second, _ = _singles(players=players, played_on=PLAYED_ON + timedelta(days=1))
    update_ratings_for_match(first.pk)

    assert update_ratings_for_match(second.pk) == LiveUpdateOutcome.UPDATED

    for player in _players(players):
        state = RatingState.objects.get(player=player)
        assert state.matches_played == 2
        assert state.live_updates_since_recalculation == 2
        assert state.last_played == PLAYED_ON + timedelta(days=1)


def _assert_nothing_written(
    history: int, events: int, snapshot: dict[str, object] | None
) -> None:
    assert RatingHistory.objects.count() == history
    assert _rating_events() == events
    assert _stored_snapshot() == snapshot


def test_a_match_earlier_than_a_stored_last_match_is_late(
    caplog: pytest.LogCaptureFixture,
) -> None:
    later, players = _singles()
    earlier, _ = _singles(players=players, played_on=PLAYED_ON - timedelta(days=3))
    update_ratings_for_match(later.pk)
    history, events, snapshot = (
        RatingHistory.objects.count(),
        _rating_events(),
        _stored_snapshot(),
    )

    with caplog.at_level(logging.INFO, logger="apps.ratings.services"):
        outcome = update_ratings_for_match(earlier.pk)

    assert outcome == LiveUpdateOutcome.LATE
    _assert_nothing_written(history, events, snapshot)
    replay_logs = [r for r in caplog.records if "replay" in r.getMessage()]
    assert len(replay_logs) == 1
    message = replay_logs[0].getMessage()
    for player in _players(players):
        assert str(player.public_code) in message
    assert (PLAYED_ON - timedelta(days=3)).isoformat() in message


def test_a_match_the_engine_finds_late_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later, players = _singles()
    earlier, _ = _singles(players=players, played_on=PLAYED_ON - timedelta(days=3))
    # The stored snapshot already holds the later match, but no rating
    # state does, so only the engine sees the earlier match is late.
    seeded = record_live(None, later)
    identity = live_config()
    EngineSnapshot.objects.create(
        parameter_fingerprint=identity.parameter_fingerprint,
        config_fingerprint=identity.config_fingerprint,
        config_version=identity.config_version,
        params_version=identity.params_version,
        engine_version="9.9.9",
        snapshot=seeded.snapshot,
    )
    calls: list[tuple[list[str], date]] = []
    monkeypatch.setattr(
        services,
        "schedule_replay",
        lambda found, earliest_date: calls.append(
            (sorted(str(p.public_code) for p in found), earliest_date)
        ),
    )

    assert update_ratings_for_match(earlier.pk) == LiveUpdateOutcome.LATE

    _assert_nothing_written(0, 0, seeded.snapshot)
    assert not RatingState.objects.exists()
    assert calls == [
        (
            sorted(str(p.public_code) for p in _players(players)),
            PLAYED_ON - timedelta(days=3),
        )
    ]


def test_a_voided_match_writes_nothing() -> None:
    match, _ = _singles()
    raw_update(match, status="voided")

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.NOT_COUNTED

    _assert_nothing_written(0, 0, None)


def test_a_match_held_by_a_possible_duplicate_flag_writes_nothing() -> None:
    match, players = _singles()
    other, _ = _singles(players=players, games=((11, 3),), best_of=1)
    MatchFlag.objects.create(
        match=match, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=other
    )

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.NOT_COUNTED

    _assert_nothing_written(0, 0, None)


def test_a_missing_match_writes_nothing() -> None:
    assert update_ratings_for_match(987654321) == LiveUpdateOutcome.MISSING
    _assert_nothing_written(0, 0, None)


def test_a_match_the_engine_rejects_saves_no_snapshot() -> None:
    # Self-reported and confirmed by one team only (ADR 0024).
    match, _ = saved_match(
        match_format=MatchFormat.SINGLES,
        divisions=MENS_SINGLES,
        confirm=[(Team.A, 0)],
    )

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.REJECTED

    assert not RatingState.objects.exists()
    assert _rating_events() == 0
    # The id is not reserved: the stored snapshot does not name it.
    assert not _stored_snapshot()


def test_a_failure_rolls_back_ratings_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, players = _singles()
    second, _ = _singles(players=players, played_on=PLAYED_ON + timedelta(days=1))
    update_ratings_for_match(first.pk)
    history, events, snapshot = (
        RatingHistory.objects.count(),
        _rating_events(),
        _stored_snapshot(),
    )
    real_apply = services.apply_rating
    calls = 0

    def failing_apply(**kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("made-up failure")
        return real_apply(**kwargs)

    monkeypatch.setattr(services, "apply_rating", failing_apply)

    with pytest.raises(RuntimeError):
        update_ratings_for_match(second.pk)

    _assert_nothing_written(history, events, snapshot)
    assert {s.matches_played for s in RatingState.objects.all()} == {1}


def test_schedule_replay_logs_codes_and_the_date_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, players = _singles()
    found = _players(players)

    with caplog.at_level(logging.INFO, logger="apps.ratings.services"):
        schedule_replay(found, date(2026, 2, 14))

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    codes = sorted(str(p.public_code) for p in found)
    assert ", ".join(codes) in message
    assert "2026-02-14" in message
    for player in found:
        assert player.display_name not in message
