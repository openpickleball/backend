"""The full replay that recalculates ratings (#47). Made-up data only.

``replay_ratings`` locks the engine snapshot row, takes the pending
replay requests, replays the whole counted ledger, stores the replay's
provenance, applies every player's replayed rating in Player row id
order, removes every rating the replay no longer produces, saves the
replayed engine's snapshot, and deletes the requests it took, in one
transaction. With no request pending it writes nothing.
"""

import logging
from datetime import date, timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, Team
from apps.matches.tests.factories import void_for_test
from apps.matches.workflow import edit_match
from apps.players.models import GenderDivision, Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings import services
from apps.ratings.engine_adapter import (
    RatingSummary,
    counted_engine_matches,
    live_config,
    run_replay,
)
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
    SUMMARY_FIELDS,
    LiveUpdateOutcome,
    ReplayOutcome,
    replay_as_of,
    replay_ratings,
    update_ratings_for_match,
)
from apps.ratings.tests.builders import MIXED, saved_match

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 3, 31)
MENS_SINGLES = {Team.A: (GenderDivision.MENS,), Team.B: (GenderDivision.MENS,)}


def _member(match: Match) -> User:
    """A member of the verified Organizer of ``match``'s Event."""
    assert match.event is not None
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=match.event.organizer, account=account)
    return account


def _tournament_match(**kwargs: Any) -> tuple[Match, dict[Team, list[Player]]]:
    if "event" not in kwargs:
        kwargs["event"] = EventFactory.create(
            kind=EventKind.TOURNAMENT, organizer=verified_organizer()
        )
    return saved_match(**kwargs)


def _singles(
    a: Player | None = None, b: Player | None = None, **kwargs: Any
) -> tuple[Match, Player, Player]:
    if a is not None and b is not None:
        kwargs["players"] = {Team.A: [a], Team.B: [b]}
    else:
        kwargs["divisions"] = MENS_SINGLES
    match, players = _tournament_match(match_format=MatchFormat.SINGLES, **kwargs)
    return match, players[Team.A][0], players[Team.B][0]


def _request(reason: ReplayReason = ReplayReason.VOID) -> RatingReplayRequest:
    request: RatingReplayRequest = RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=reason
    )
    return request


def _snapshot_row() -> EngineSnapshot | None:
    return EngineSnapshot.objects.filter(
        parameter_fingerprint=live_config().parameter_fingerprint
    ).first()


def _expected() -> dict[tuple[str, str], RatingSummary]:
    """A direct full replay of the current counted ledger, as of AS_OF."""
    run = run_replay(counted_engine_matches(), AS_OF)
    return {(s.player_id, s.rating_format): s for s in run.summaries}


def _stored() -> dict[tuple[str, str], RatingState]:
    return {
        (str(state.player.public_code), state.rating_format): state
        for state in RatingState.objects.select_related("player")
    }


def _assert_equal_to_a_full_replay() -> None:
    expected = _expected()
    stored = _stored()
    assert set(stored) == set(expected)
    for key, summary in expected.items():
        for name in SUMMARY_FIELDS:
            assert getattr(stored[key], name) == getattr(summary, name), (key, name)


def _three_live_singles() -> tuple[list[Match], Player, Player, Player]:
    """A plays B, then C, then B again, each rated live."""
    first, a, b = _singles(played_on=date(2026, 3, 1))
    c: Player = PlayerFactory.create(gender_division=GenderDivision.MENS)
    second, _, _ = _singles(a, c, played_on=date(2026, 3, 8))
    third, _, _ = _singles(a, b, played_on=date(2026, 3, 15))
    for match in (first, second, third):
        assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UPDATED
    return [first, second, third], a, b, c


# Nothing requested


def test_with_no_request_pending_nothing_is_written() -> None:
    match, _, _ = _singles()
    update_ratings_for_match(match.pk)
    history = RatingHistory.objects.count()
    events = AuditEvent.objects.count()
    row = _snapshot_row()
    assert row is not None

    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.NOTHING_REQUESTED

    assert not RatingReplay.objects.exists()
    assert RatingHistory.objects.count() == history
    assert AuditEvent.objects.count() == events
    after = _snapshot_row()
    assert after is not None
    assert after.snapshot == row.snapshot


def test_with_no_request_and_no_snapshot_row_nothing_is_created() -> None:
    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.NOTHING_REQUESTED

    assert not EngineSnapshot.objects.exists()


# A replay


def test_a_replay_rates_every_counted_match_from_scratch() -> None:
    _singles(played_on=date(2026, 3, 1))
    _tournament_match(divisions=MIXED, played_on=date(2026, 3, 8))
    _request()

    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.REPLAYED

    _assert_equal_to_a_full_replay()
    replay = RatingReplay.objects.get()
    assert replay.as_of == AS_OF
    assert replay.matches_counted == 2
    for state in RatingState.objects.all():
        assert state.calculated_as_of == AS_OF
        assert state.live_updates_since_recalculation == 0
        assert not state.provisional
        [row] = RatingHistory.objects.filter(
            player=state.player, rating_format=state.rating_format
        )
        assert row.kind == RatingChangeKind.REPLAY
        assert row.replay == replay


def test_a_replay_saves_its_snapshot_on_the_row() -> None:
    match, _, _ = _singles()
    _request()

    replay_ratings(as_of=AS_OF)

    row = _snapshot_row()
    assert row is not None
    assert row.snapshot == run_replay(counted_engine_matches(), AS_OF).snapshot
    assert str(match.public_code) in row.snapshot["processed_match_ids"]
    labels = live_config()
    assert row.config_fingerprint == labels.config_fingerprint
    assert row.config_version == labels.config_version
    assert row.params_version == labels.params_version


def test_a_replay_deletes_the_requests_it_took() -> None:
    _singles()
    _request(ReplayReason.VOID)
    _request(ReplayReason.LATE)

    replay_ratings(as_of=AS_OF)

    assert not RatingReplayRequest.objects.exists()
    assert RatingReplay.objects.count() == 1


def test_a_request_stored_during_the_run_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _singles()
    _request()
    late: list[RatingReplayRequest] = []

    def run_and_request(*args: Any, **kwargs: Any) -> Any:
        late.append(_request(ReplayReason.EDIT))
        return run_replay(*args, **kwargs)

    monkeypatch.setattr("apps.ratings.services.run_replay", run_and_request)

    replay_ratings(as_of=AS_OF)

    assert list(RatingReplayRequest.objects.all()) == late


def test_a_second_run_finds_nothing_requested() -> None:
    _singles()
    _request()
    replay_ratings(as_of=AS_OF)
    history = RatingHistory.objects.count()

    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.NOTHING_REQUESTED

    assert RatingReplay.objects.count() == 1
    assert RatingHistory.objects.count() == history


def test_a_replay_logs_dates_and_counts_only(caplog: pytest.LogCaptureFixture) -> None:
    _, a, b = _singles()
    _request()

    with caplog.at_level(logging.INFO, logger="apps.ratings.services"):
        replay_ratings(as_of=AS_OF)

    [record] = [r for r in caplog.records if r.name == "apps.ratings.services"]
    message = record.getMessage()
    assert AS_OF.isoformat() in message
    assert "2026-03-01" in message
    for player in (a, b):
        assert str(player.public_code) not in message
        assert player.display_name not in message


# Voids, edits, and late matches


def test_a_void_equals_a_full_replay_without_the_match() -> None:
    (_, second, _), a, b, _ = _three_live_singles()
    void_for_test(second, actor=_member(second))
    _request()

    assert replay_ratings(as_of=AS_OF) == ReplayOutcome.REPLAYED

    _assert_equal_to_a_full_replay()
    assert RatingState.objects.get(player=a).matches_played == 2
    assert RatingState.objects.get(player=b).matches_played == 2


def test_a_void_removes_a_rating_with_no_counted_match_left() -> None:
    (_, second, _), _, _, c = _three_live_singles()
    removed = RatingState.objects.get(player=c)
    before = {name: getattr(removed, name) for name in services.VALUE_FIELDS}
    void_for_test(second, actor=_member(second))
    _request()

    replay_ratings(as_of=AS_OF)

    assert not RatingState.objects.filter(player=c).exists()
    replay = RatingReplay.objects.get()
    [row] = RatingHistory.objects.filter(player=c, kind=RatingChangeKind.REMOVED)
    assert row.replay == replay
    assert row.match is None
    assert row.rating_format == "singles"
    for name, value in before.items():
        assert getattr(row, name) == value
    event = (
        AuditEvent.objects.filter(
            action=AuditAction.RATING_CHANGE, target_object_id=str(removed.pk)
        )
        .order_by("-pk")
        .first()
    )
    assert event is not None
    assert event.after is None
    assert event.before is not None


def test_an_edit_equals_a_full_replay_of_the_current_ledger() -> None:
    (first, _, _), a, b, _ = _three_live_singles()
    correction = edit_match(
        first,
        actor=_member(first),
        format=MatchFormat.SINGLES,
        team_a=[b],
        team_b=[a],
        games=((11, 4), (11, 6)),
        played_on=date(2026, 3, 2),
    )
    assert correction.status == MatchStatus.CONFIRMED
    _request(ReplayReason.EDIT)

    replay_ratings(as_of=AS_OF)

    _assert_equal_to_a_full_replay()


def test_a_backdated_match_ends_equal_to_a_full_replay() -> None:
    (_, _, _), a, b, _ = _three_live_singles()
    backdated, _, _ = _singles(a, b, played_on=date(2026, 3, 3))
    assert update_ratings_for_match(backdated.pk) == LiveUpdateOutcome.LATE
    _request(ReplayReason.LATE)

    replay_ratings(as_of=AS_OF)

    _assert_equal_to_a_full_replay()
    assert RatingState.objects.get(player=a).matches_played == 4


# After a replay


def test_a_live_update_after_a_replay_continues_from_its_snapshot() -> None:
    (_, _, _), a, b, _ = _three_live_singles()
    _request()
    replay_ratings(as_of=AS_OF)
    later, _, _ = _singles(a, b, played_on=AS_OF + timedelta(days=5))

    assert update_ratings_for_match(later.pk) == LiveUpdateOutcome.UPDATED

    state = RatingState.objects.get(player=a)
    assert state.live_updates_since_recalculation == 1
    assert state.calculated_as_of == AS_OF
    assert state.matches_played == 4


def test_a_live_update_of_a_match_the_replay_counted_changes_nothing() -> None:
    match, _, _ = _singles(played_on=date(2026, 3, 1))
    _request()
    replay_ratings(as_of=AS_OF)
    history = RatingHistory.objects.count()

    assert update_ratings_for_match(match.pk) == LiveUpdateOutcome.UNCHANGED

    assert RatingHistory.objects.count() == history


# Failures


def test_a_failure_rolls_everything_back_and_keeps_the_requests(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (_, second, _), _, _, _ = _three_live_singles()
    void_for_test(second, actor=_member(second))
    _request()
    requests = list(RatingReplayRequest.objects.order_by("pk"))
    states = {
        key: {name: getattr(state, name) for name in services.VALUE_FIELDS}
        for key, state in _stored().items()
    }
    history = RatingHistory.objects.count()
    events = AuditEvent.objects.count()
    row = _snapshot_row()
    assert row is not None
    real_apply = services.apply_rating
    calls = 0

    def failing_apply(**kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("made-up failure text")
        return real_apply(**kwargs)

    monkeypatch.setattr(services, "apply_rating", failing_apply)

    with pytest.raises(RuntimeError):
        replay_ratings(as_of=AS_OF)

    assert not RatingReplay.objects.exists()
    assert RatingHistory.objects.count() == history
    assert AuditEvent.objects.count() == events
    assert {
        key: {name: getattr(state, name) for name in services.VALUE_FIELDS}
        for key, state in _stored().items()
    } == states
    after = _snapshot_row()
    assert after is not None
    assert after.snapshot == row.snapshot
    assert list(RatingReplayRequest.objects.order_by("pk")) == requests
    [record] = [
        r
        for r in caplog.records
        if r.name == "apps.ratings.services" and r.levelno == logging.ERROR
    ]
    assert "RuntimeError" in record.getMessage()
    assert "made-up failure text" not in record.getMessage()
    assert record.exc_info is None


def test_a_summary_for_an_unknown_player_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _singles()
    _request()

    def run_with_a_stranger(*args: Any, **kwargs: Any) -> Any:
        run = run_replay(*args, **kwargs)
        stranger = RatingSummary(
            **{**vars(run.summaries[0]), "player_id": "PZZZZZZZZZ"}
        )
        return type(run)(**{**vars(run), "summaries": (*run.summaries, stranger)})

    monkeypatch.setattr("apps.ratings.services.run_replay", run_with_a_stranger)

    with pytest.raises(services.RatingApplyError):
        replay_ratings(as_of=AS_OF)

    assert not RatingReplay.objects.exists()
    assert RatingReplayRequest.objects.count() == 1


# The replay date


def test_the_replay_date_is_today_in_utc_by_default() -> None:
    _singles(played_on=date(2026, 3, 1))

    assert replay_as_of() == timezone.now().date()


def test_the_replay_date_covers_a_counted_match_dated_after_today() -> None:
    tomorrow = timezone.now().date() + timedelta(days=1)
    _singles(played_on=tomorrow)

    assert replay_as_of() == tomorrow


def test_without_a_date_the_replay_uses_the_replay_date() -> None:
    _singles(played_on=date(2026, 3, 1))
    _request()

    replay_ratings()

    assert RatingReplay.objects.get().as_of == replay_as_of()
