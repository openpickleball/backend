"""The ``recompute_ratings`` command (#48, ADR 0058). Made-up data only.

The command rebuilds every rating with a full replay of the counted
ledger, in one transaction like the regular replay. A dry run writes
nothing. Only a dry run takes ``--format``, an earlier ``--as-of``, or
another ``--spec-version``.
"""

import logging
from datetime import date
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from apps.audit.models import AuditEvent
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.tests.factories import raw_update
from apps.players.models import GenderDivision, Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings import services
from apps.ratings.engine_adapter import counted_engine_matches, live_config, run_replay
from apps.ratings.models import (
    EngineSnapshot,
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingReplayRequest,
    RatingState,
    ReplayReason,
)
from apps.ratings.services import (
    LiveUpdateOutcome,
    RecomputeChangeKind,
    RecomputeRefused,
    recompute_ratings,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import MIXED, saved_match

pytestmark = pytest.mark.django_db

MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}
LAST_PLAYED = date(2026, 3, 15)


def _match(**kwargs: Any) -> tuple[Match, dict[Team, list[Player]]]:
    kwargs.setdefault("event", EventFactory.create(kind=EventKind.TOURNAMENT))
    return saved_match(**kwargs)


def _singles(a: Player, b: Player, played_on: date) -> Match:
    match, _ = _match(
        match_format=MatchFormat.SINGLES,
        players={Team.A: [a], Team.B: [b]},
        played_on=played_on,
    )
    return match


def _seeded() -> tuple[list[Match], Player]:
    """Singles and mixed doubles matches, each rated live, in date order.

    A plays B, then C, then B again in singles; four players play mixed
    doubles twice. C's last match is before the others' (2026-03-08).
    """
    a, b, c = (
        PlayerFactory.create(gender_division=GenderDivision.MENS) for _ in range(3)
    )
    first_doubles, team = _match(divisions=MIXED, played_on=date(2026, 3, 1))
    matches = [
        _singles(a, b, date(2026, 3, 1)),
        first_doubles,
        _singles(a, c, date(2026, 3, 8)),
        _singles(a, b, LAST_PLAYED),
        _match(players=team, played_on=LAST_PLAYED)[0],
    ]
    for match in matches:
        assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED
    return matches, c


def _states() -> dict[tuple[str, str], dict[str, object]]:
    return {
        (str(state.player.public_code), state.rating_format): {
            name: getattr(state, name) for name in services.VALUE_FIELDS
        }
        for state in RatingState.objects.select_related("player")
    }


def _counts() -> dict[str, int]:
    return {
        "states": RatingState.objects.count(),
        "history": RatingHistory.objects.count(),
        "replays": RatingReplay.objects.count(),
        "snapshots": EngineSnapshot.objects.count(),
        "requests": RatingReplayRequest.objects.count(),
        "events": AuditEvent.objects.count(),
    }


def _snapshot() -> dict[str, object] | None:
    row = EngineSnapshot.objects.filter(
        parameter_fingerprint=live_config().parameter_fingerprint
    ).first()
    return None if row is None else row.snapshot


def _run(*args: str) -> str:
    out = StringIO()
    call_command("recompute_ratings", *args, stdout=out)
    return out.getvalue()


# The acceptance check: the recompute matches the incremental results.


def test_a_recompute_matches_the_live_updates() -> None:
    _, c = _seeded()
    live = _states()

    _run("--as-of", LAST_PLAYED.isoformat())

    recomputed = _states()
    assert set(recomputed) == set(live)
    inactive = set()
    for key, before in live.items():
        after = recomputed[key]
        for name in ("rating", "matches_played", "last_played", "established"):
            assert after[name] == before[name], (key, name)
        if before["last_played"] == LAST_PLAYED:
            assert after["rd"] == before["rd"], key
            assert after["reliability"] == before["reliability"], key
        else:
            # The engine's inactivity rule: RD grows with time since the
            # last match, so the replay's is at least the live one.
            assert isinstance(after["rd"], float)
            assert isinstance(before["rd"], float)
            assert after["rd"] >= before["rd"], key
            inactive.add(key)
        assert after["calculated_as_of"] == LAST_PLAYED
        assert after["live_updates_since_recalculation"] == 0
    # C is the player whose last match is before the replay date.
    assert inactive == {(str(c.public_code), "singles")}


def test_a_recompute_stores_replay_rows_through_the_services() -> None:
    _seeded()
    states = RatingState.objects.count()

    _run("--as-of", LAST_PLAYED.isoformat())

    [replay] = RatingReplay.objects.all()
    assert replay.as_of == LAST_PLAYED
    rows = RatingHistory.objects.filter(replay=replay)
    assert rows.count() == states
    assert {row.kind for row in rows} == {RatingChangeKind.REPLAY}
    row = EngineSnapshot.objects.get(
        parameter_fingerprint=live_config().parameter_fingerprint
    )
    assert row.snapshot == run_replay(counted_engine_matches(), LAST_PLAYED).snapshot


def test_a_recompute_runs_without_a_replay_request() -> None:
    _seeded()
    assert not RatingReplayRequest.objects.exists()

    _run()

    assert RatingReplay.objects.count() == 1


def test_a_recompute_defaults_to_the_replay_date() -> None:
    _seeded()

    out = _run()

    assert RatingReplay.objects.get().as_of == services.replay_as_of()
    assert services.replay_as_of().isoformat() in out


# Spec version


def test_new_history_rows_carry_the_new_spec_version() -> None:
    _seeded()
    old = RatingHistory.objects.count()
    assert set(RatingState.objects.values_list("spec_version", flat=True)) == {
        "1.0.0rc1"
    }

    with override_settings(OPEN_RATING_SPEC_VERSION="1.0.0"):
        out = _run("--as-of", LAST_PLAYED.isoformat())

    new = RatingHistory.objects.order_by("pk")[old:]
    assert new
    assert {row.spec_version for row in new} == {"1.0.0"}
    assert RatingReplay.objects.get().spec_version == "1.0.0"
    assert set(RatingState.objects.values_list("spec_version", flat=True)) == {"1.0.0"}
    assert "spec version 1.0.0" in out


def test_the_spec_version_option_naming_the_setting_is_allowed() -> None:
    _seeded()

    _run("--spec-version", "1.0.0rc1")

    assert RatingReplay.objects.get().spec_version == "1.0.0rc1"


# Re-running


def test_running_it_again_changes_no_rating() -> None:
    _seeded()
    _run("--as-of", LAST_PLAYED.isoformat())
    history = RatingHistory.objects.count()
    states = _states()

    out = _run("--as-of", LAST_PLAYED.isoformat())

    assert RatingHistory.objects.count() == history
    assert _states() == states
    assert RatingReplay.objects.count() == 2
    assert f"Ratings unchanged: {len(states)}" in out
    assert "Ratings changed: 0" in out


# Dry run


def test_a_dry_run_writes_nothing() -> None:
    _seeded()
    RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=ReplayReason.VOID
    )
    counts = _counts()
    states = _states()
    snapshot = _snapshot()

    out = _run("--dry-run", "--as-of", LAST_PLAYED.isoformat())

    assert _counts() == counts
    assert _states() == states
    assert _snapshot() == snapshot
    assert out.startswith("Dry run, nothing written")
    assert f"Ratings changed: {len(states)}" in out
    assert "Replay requests cleared" not in out


def test_a_dry_run_creates_no_snapshot_row() -> None:
    _match(match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES)
    assert not EngineSnapshot.objects.exists()

    out = _run("--dry-run")

    assert _counts() == dict.fromkeys(_counts(), 0)
    assert "Ratings created: 2" in out


def test_a_dry_run_predicts_what_the_write_does() -> None:
    (_, _, second_singles, _, _), _ = _seeded()
    raw_update(second_singles, status=MatchStatus.VOIDED)

    planned = recompute_ratings(as_of=LAST_PLAYED, dry_run=True)
    written = recompute_ratings(as_of=LAST_PLAYED)

    assert planned.changes == written.changes
    assert written.count(RecomputeChangeKind.REMOVED) == 1
    assert written.count(RecomputeChangeKind.CHANGED) > 0


def test_a_dry_run_lists_changes_by_open_code_at_verbosity_two() -> None:
    (_, _, second_singles, _, _), c = _seeded()
    raw_update(second_singles, status=MatchStatus.VOIDED)

    out = _run("--dry-run", "--as-of", LAST_PLAYED.isoformat(), "--verbosity", "2")

    assert f"removed {c.public_code} singles: " in out
    assert "Ratings removed: 1" in out
    assert c.display_name not in out
    assert RatingState.objects.filter(player=c).exists()


def test_a_dry_run_narrows_to_one_format() -> None:
    _seeded()

    result = recompute_ratings(dry_run=True, rating_format="mixed_doubles")

    assert result.changes
    assert {change.rating_format for change in result.changes} == {"mixed_doubles"}
    out = _run("--dry-run", "--format", "singles")
    assert "spec version 1.0.0rc1, singles." in out


def test_a_dry_run_takes_an_earlier_date_and_another_spec_version() -> None:
    _seeded()
    counts = _counts()

    result = recompute_ratings(
        as_of=date(2026, 3, 8), spec_version="9.9.9", dry_run=True
    )

    assert result.as_of == date(2026, 3, 8)
    assert result.spec_version == "9.9.9"
    assert result.matches_counted < 5
    assert _counts() == counts


# Refusals


@pytest.mark.parametrize(
    "args",
    [
        ("--format", "singles"),
        ("--spec-version", "1.0.0"),
        ("--as-of", "2026-03-14"),
    ],
)
def test_options_for_a_dry_run_are_refused_for_a_write(args: tuple[str, ...]) -> None:
    _seeded()
    counts = _counts()
    states = _states()

    with pytest.raises(CommandError, match="only in a dry run"):
        _run(*args)

    assert _counts() == counts
    assert _states() == states


def test_a_refused_write_on_an_empty_database_creates_no_snapshot_row() -> None:
    _match(match_format=MatchFormat.SINGLES, divisions=MENS_SINGLES)

    with pytest.raises(RecomputeRefused):
        recompute_ratings(as_of=date(2026, 2, 1))

    assert not EngineSnapshot.objects.exists()


def test_an_unknown_format_is_refused() -> None:
    with pytest.raises(RecomputeRefused, match="Unknown rating format"):
        recompute_ratings(rating_format="triples", dry_run=True)


def test_a_malformed_date_is_refused() -> None:
    with pytest.raises(CommandError, match="--as-of must be a date"):
        _run("--as-of", "15/03/2026")


# Replay requests


def test_a_recompute_clears_the_requests_made_before_it() -> None:
    _seeded()
    for reason in (ReplayReason.VOID, ReplayReason.LATE):
        RatingReplayRequest.objects.create(
            earliest_date=date(2026, 3, 1), reason=reason
        )

    out = _run()

    assert not RatingReplayRequest.objects.exists()
    assert "Replay requests cleared: 2" in out


def test_a_request_stored_during_the_recompute_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seeded()
    RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=ReplayReason.VOID
    )
    late: list[RatingReplayRequest] = []

    def run_and_request(*args: Any, **kwargs: Any) -> Any:
        late.append(
            RatingReplayRequest.objects.create(
                earliest_date=date(2026, 3, 1), reason=ReplayReason.EDIT
            )
        )
        return run_replay(*args, **kwargs)

    monkeypatch.setattr("apps.ratings.services.run_replay", run_and_request)

    result = recompute_ratings()

    assert result.requests_cleared == 1
    assert list(RatingReplayRequest.objects.all()) == late


# Failures


def test_a_failure_rolls_everything_back_and_hides_the_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seeded()
    RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=ReplayReason.VOID
    )
    counts = _counts()
    states = _states()
    snapshot = _snapshot()
    real_apply = services.apply_rating
    calls = 0

    def failing_apply(**kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("made-up failure text")
        return real_apply(**kwargs)

    monkeypatch.setattr(services, "apply_rating", failing_apply)

    with pytest.raises(CommandError) as raised:
        _run()

    assert "RuntimeError" in str(raised.value)
    assert "made-up failure text" not in str(raised.value)
    assert _counts() == counts
    assert _states() == states
    assert _snapshot() == snapshot
    [record] = [
        r
        for r in caplog.records
        if r.name == "apps.ratings.services" and r.levelno == logging.ERROR
    ]
    assert "RuntimeError" in record.getMessage()
    assert "made-up failure text" not in record.getMessage()
    assert record.exc_info is None


def test_the_log_holds_counts_and_dates_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    matches, _ = _seeded()
    players = {p for m in matches for p in m.participants.all()}

    with caplog.at_level(logging.INFO, logger="apps.ratings.services"):
        _run("--as-of", LAST_PLAYED.isoformat())

    [record] = [
        r
        for r in caplog.records
        if r.name == "apps.ratings.services" and "Recompute" in r.getMessage()
    ]
    message = record.getMessage()
    assert LAST_PLAYED.isoformat() in message
    for participant in players:
        assert str(participant.player.public_code) not in message
        assert participant.player.display_name not in message
