"""Storing rating changes and replays (#45).

``apply_rating`` stores the engine's summary for one Player and rating
format: it creates or changes the ``RatingState``, appends one
``RatingHistory`` row naming the cause, and records one ``rating.change``
Audit Event, all in one transaction. The same values again change
nothing. ``record_replay`` stores one replay's provenance (hard rule 4).
Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.test import override_settings

from apps.audit.models import AuditAction, AuditEvent
from apps.matches.models import (
    Game,
    Match,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    RatingFormat,
    Team,
)
from apps.matches.tests.factories import DoublesMatchFactory, MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.engine_adapter import RatingSummary, engine_version
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
)
from apps.ratings.services import (
    VALUE_FIELDS,
    RatingApplyError,
    apply_rating,
    record_replay,
)
from apps.ratings.tests.factories import (
    MADE_UP_ENGINE_VERSION,
    rating_summary,
    replay_provenance,
    replay_values,
)

pytestmark = pytest.mark.django_db


def _played(match: Match) -> Player:
    participant = match.participants.first()
    assert participant is not None
    player: Player = participant.player
    return player


@pytest.fixture
def match() -> Match:
    made: Match = MatchFactory.create(status=MatchStatus.CONFIRMED)
    return made


def _another_match(player: Player) -> Match:
    """A second confirmed singles Match that counts, with ``player`` in it."""
    other: Match = MatchFactory.create(
        status=MatchStatus.CONFIRMED, participants=False, games=False
    )
    MatchParticipant.objects.create(match=other, player=player, team=Team.A)
    MatchParticipant.objects.create(
        match=other, player=PlayerFactory.create(), team=Team.B
    )
    Game.objects.create(match=other, game_number=1, team_a_score=11, team_b_score=4)
    return other


@pytest.fixture
def player(match: Match) -> Player:
    return _played(match)


def _singles(player: Player, **overrides: Any) -> RatingSummary:
    return rating_summary(
        player, rating_format=str(RatingFormat.SINGLES.value), **overrides
    )


def _events() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).order_by(
            "created_at", "id"
        )
    )


def _live(player: Player, match: Match, summary: RatingSummary) -> RatingHistory:
    history = apply_rating(
        player=player,
        summary=summary,
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
        delta=0.031,
    )
    assert history is not None
    return history


# The spec version setting


def test_the_spec_version_is_the_rating_spec_release_candidate() -> None:
    assert settings.OPEN_RATING_SPEC_VERSION == "1.0.0rc1"


# Live updates


def test_a_live_update_creates_the_state_and_one_history_row(
    player: Player, match: Match
) -> None:
    summary = _singles(player)

    history = _live(player, match, summary)

    state = RatingState.objects.get(player=player, rating_format="singles")
    for name in (
        "rating",
        "rd",
        "reliability",
        "imported_reliability",
        "established",
        "matches_played",
        "last_played",
        "calculated_as_of",
        "live_updates_since_recalculation",
        "config_version",
        "params_version",
        "parameter_fingerprint",
    ):
        assert getattr(state, name) == getattr(summary, name), name
        assert getattr(history, name) == getattr(summary, name), name
    assert history.kind == RatingChangeKind.LIVE_UPDATE
    assert history.match == match
    assert history.replay is None
    assert history.delta == 0.031
    assert history.player == player
    assert history.rating_format == "singles"
    assert RatingHistory.objects.count() == 1


def test_a_live_update_carries_the_installed_engine_and_spec_versions(
    player: Player, match: Match
) -> None:
    history = _live(player, match, _singles(player))

    state = RatingState.objects.get(player=player)
    assert history.engine_version == state.engine_version == engine_version()
    assert (
        history.spec_version == state.spec_version == settings.OPEN_RATING_SPEC_VERSION
    )


@override_settings(OPEN_RATING_SPEC_VERSION="spec-2027-test")
def test_the_spec_version_comes_from_the_setting(player: Player, match: Match) -> None:
    history = _live(player, match, _singles(player))

    assert history.spec_version == "spec-2027-test"
    assert RatingState.objects.get(player=player).spec_version == "spec-2027-test"


def test_imported_reliability_is_stored_apart(player: Player, match: Match) -> None:
    history = _live(
        player, match, _singles(player, reliability=12.5, imported_reliability=70.0)
    )

    assert history.reliability == 12.5
    assert history.imported_reliability == 70.0


def test_a_category_match_may_update_overall_doubles() -> None:
    mixed: Match = DoublesMatchFactory.create(
        rating_format=RatingFormat.MIXED_DOUBLES, status=MatchStatus.CONFIRMED
    )
    player = _played(mixed)

    for rating_format in ("mixed_doubles", "doubles"):
        history = _live(
            player, mixed, rating_summary(player, rating_format=rating_format)
        )
        assert history.rating_format == rating_format

    assert RatingState.objects.filter(player=player).count() == 2


# Changes, and no change


def test_a_change_updates_the_state_and_appends_history(
    player: Player, match: Match
) -> None:
    _live(player, match, _singles(player, rating=3.5))
    second = _live(
        player,
        _another_match(player),
        _singles(player, rating=3.6, matches_played=4),
    )

    state = RatingState.objects.get(player=player)
    assert state.rating == 3.6
    assert state.matches_played == 4
    assert RatingState.objects.filter(player=player).count() == 1
    assert list(RatingHistory.objects.values_list("rating", flat=True)) == [3.5, 3.6]
    assert second.rating == 3.6
    assert len(_events()) == 2


def test_the_same_values_again_change_nothing(player: Player, match: Match) -> None:
    summary = _singles(player)
    _live(player, match, summary)
    state = RatingState.objects.get(player=player)

    again = apply_rating(
        player=player,
        summary=summary,
        kind=RatingChangeKind.LIVE_UPDATE,
        match=_another_match(player),
        delta=0.031,
    )

    assert again is None
    assert RatingHistory.objects.count() == 1
    assert len(_events()) == 1
    unchanged = RatingState.objects.get(player=player)
    assert unchanged.updated_at == state.updated_at


def test_a_new_spec_version_is_a_change(player: Player, match: Match) -> None:
    summary = _singles(player)
    _live(player, match, summary)

    with override_settings(OPEN_RATING_SPEC_VERSION="spec-next-test"):
        history = _live(player, _another_match(player), summary)

    assert history.spec_version == "spec-next-test"
    assert RatingHistory.objects.count() == 2


# The Audit Event


def _expected_values(summary: RatingSummary, engine: str, spec: str) -> dict[str, Any]:
    return {
        "rating": summary.rating,
        "rd": summary.rd,
        "reliability": summary.reliability,
        "imported_reliability": summary.imported_reliability,
        "established": summary.established,
        "matches_played": summary.matches_played,
        "last_played": None
        if summary.last_played is None
        else summary.last_played.isoformat(),
        "calculated_as_of": None
        if summary.calculated_as_of is None
        else summary.calculated_as_of.isoformat(),
        "live_updates_since_recalculation": summary.live_updates_since_recalculation,
        "config_version": summary.config_version,
        "params_version": summary.params_version,
        "parameter_fingerprint": summary.parameter_fingerprint,
        "engine_version": engine,
        "spec_version": spec,
    }


def test_value_fields_are_the_stored_summary_and_versions() -> None:
    assert set(VALUE_FIELDS) == set(
        _expected_values(rating_summary(PlayerFactory.create()), "", "")
    )


def test_each_change_records_one_rating_change_event(
    player: Player, match: Match
) -> None:
    first = _singles(player, rating=3.5)
    second = _singles(player, rating=3.6)
    second_match = _another_match(player)
    _live(player, match, first)
    _live(player, second_match, second)

    events = _events()
    state = RatingState.objects.get(player=player)
    assert len(events) == 2
    for event in events:
        assert event.target == state
        assert event.actor_user is None
        assert event.actor_application is None
        assert event.reason == ""
    created, changed = events
    first_values = _expected_values(
        first, engine_version(), settings.OPEN_RATING_SPEC_VERSION
    )
    second_values = _expected_values(
        second, engine_version(), settings.OPEN_RATING_SPEC_VERSION
    )
    assert created.before is None
    assert created.after == {
        **first_values,
        "kind": "live_update",
        "match": match.public_code,
        "replay": None,
        "player": player.public_code,
    }
    assert changed.before == first_values
    assert changed.after == {
        **second_values,
        "kind": "live_update",
        "match": second_match.public_code,
        "replay": None,
        "player": player.public_code,
    }


def test_a_replay_change_event_names_the_replay(player: Player) -> None:
    replay = record_replay(replay_provenance())
    summary = _singles(player, calculated_as_of=date(2026, 3, 31))

    apply_rating(
        player=player, summary=summary, kind=RatingChangeKind.REPLAY, replay=replay
    )

    (event,) = _events()
    assert event.after == {
        **_expected_values(summary, MADE_UP_ENGINE_VERSION, replay.spec_version),
        "kind": "replay",
        "match": None,
        "replay": replay.pk,
        "player": player.public_code,
    }


# Replays


def test_record_replay_stores_every_provenance_field() -> None:
    provenance = replay_provenance()

    replay = record_replay(provenance)

    replay.refresh_from_db()
    assert {name: getattr(replay, name) for name in replay_values()} == {
        **replay_values(),
        "net_rating_created": provenance.net_rating_created,
        "spec_version": settings.OPEN_RATING_SPEC_VERSION,
    }
    assert RatingReplay.objects.count() == 1
    assert _events() == []


def test_record_replay_stores_an_unlabelled_parameter_set() -> None:
    replay = record_replay(replay_provenance(params_version=None))

    assert replay.params_version is None


def test_a_replay_row_takes_its_versions_from_the_replay(player: Player) -> None:
    with override_settings(OPEN_RATING_SPEC_VERSION="spec-at-replay-test"):
        replay = record_replay(replay_provenance())

    history = apply_rating(
        player=player,
        summary=_singles(player, calculated_as_of=date(2026, 3, 31)),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
    )

    assert history is not None
    assert history.replay == replay
    assert history.match is None
    assert history.delta is None
    assert history.engine_version == MADE_UP_ENGINE_VERSION
    assert history.spec_version == "spec-at-replay-test"
    state = RatingState.objects.get(player=player)
    assert state.engine_version == MADE_UP_ENGINE_VERSION
    assert state.spec_version == "spec-at-replay-test"
    assert state.provisional is False


def test_a_live_update_after_a_replay_is_provisional(
    player: Player, match: Match
) -> None:
    replay = record_replay(replay_provenance())
    apply_rating(
        player=player,
        summary=_singles(player, calculated_as_of=date(2026, 3, 31)),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
    )

    _live(
        player,
        match,
        _singles(
            player,
            calculated_as_of=date(2026, 3, 31),
            live_updates_since_recalculation=1,
        ),
    )

    assert RatingState.objects.get(player=player).provisional is True


# Refusals: nothing is written, and the message names no value


def _nothing_written() -> None:
    assert not RatingState.objects.exists()
    assert not RatingHistory.objects.exists()
    assert _events() == []


def _refused(player: Player, **kwargs: Any) -> str:
    with pytest.raises(RatingApplyError) as caught:
        apply_rating(player=player, **kwargs)
    _nothing_written()
    message = str(caught.value)
    assert player.public_code is None or player.public_code not in message
    return message


def test_an_unsaved_player_is_refused(player: Player, match: Match) -> None:
    unsaved = Player(display_name="Made Up")

    _refused(
        unsaved,
        summary=_singles(player),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
    )


def test_a_summary_for_another_player_is_refused(match: Match) -> None:
    participants = list(match.participants.all())
    player, other = participants[0].player, participants[1].player

    _refused(
        player,
        summary=_singles(other),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
    )


def test_an_unknown_rating_format_is_refused(player: Player, match: Match) -> None:
    _refused(
        player,
        summary=rating_summary(player, rating_format="bogus"),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
    )


def test_an_unknown_kind_is_refused(player: Player, match: Match) -> None:
    _refused(player, summary=_singles(player), kind="guess", match=match)


def test_a_live_update_without_a_match_is_refused(player: Player) -> None:
    _refused(player, summary=_singles(player), kind=RatingChangeKind.LIVE_UPDATE)


def test_a_live_update_with_a_replay_is_refused(player: Player, match: Match) -> None:
    replay = record_replay(replay_provenance())

    _refused(
        player,
        summary=_singles(player),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
        replay=replay,
    )


def test_a_replay_row_without_a_replay_is_refused(player: Player) -> None:
    _refused(player, summary=_singles(player), kind=RatingChangeKind.REPLAY)


def test_a_replay_row_with_a_match_is_refused(player: Player, match: Match) -> None:
    replay = record_replay(replay_provenance())

    _refused(
        player,
        summary=_singles(player),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
        match=match,
    )


def test_a_replay_row_with_a_delta_is_refused(player: Player) -> None:
    replay = record_replay(replay_provenance())

    _refused(
        player,
        summary=_singles(player),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
        delta=0.5,
    )


def test_an_unsaved_match_is_refused(player: Player) -> None:
    unsaved = Match(format=MatchFormat.SINGLES, played_on=date(2026, 3, 1))

    _refused(
        player,
        summary=_singles(player),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=unsaved,
    )


def test_an_unsaved_replay_is_refused(player: Player) -> None:
    unsaved = RatingReplay(**replay_values())

    _refused(
        player,
        summary=_singles(player),
        kind=RatingChangeKind.REPLAY,
        replay=unsaved,
    )


def test_a_match_the_player_did_not_play_in_is_refused(match: Match) -> None:
    outsider: Player = PlayerFactory.create()

    _refused(
        outsider,
        summary=_singles(outsider),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
    )


def test_a_live_update_in_another_format_is_refused(
    player: Player, match: Match
) -> None:
    _refused(
        player,
        summary=rating_summary(player, rating_format="doubles"),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
    )


def test_a_category_match_cannot_update_another_category() -> None:
    mixed: Match = DoublesMatchFactory.create(
        rating_format=RatingFormat.MIXED_DOUBLES, status=MatchStatus.CONFIRMED
    )
    player = _played(mixed)

    _refused(
        player,
        summary=rating_summary(player, rating_format="doubles_men"),
        kind=RatingChangeKind.LIVE_UPDATE,
        match=mixed,
    )


def test_a_value_out_of_range_writes_nothing(player: Player, match: Match) -> None:
    with pytest.raises(ValidationError) as caught:
        apply_rating(
            player=player,
            summary=_singles(player, reliability=101.0),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
        )

    assert "reliability" in caught.value.message_dict
    _nothing_written()


def test_a_bad_change_to_an_existing_state_writes_nothing(
    player: Player, match: Match
) -> None:
    _live(player, match, _singles(player))

    with pytest.raises(ValidationError):
        apply_rating(
            player=player,
            summary=_singles(player, reliability=101.0),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=_another_match(player),
        )

    assert RatingState.objects.get(player=player).reliability == 41.5
    assert RatingHistory.objects.count() == 1
    assert len(_events()) == 1
