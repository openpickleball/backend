"""Guards on storing ratings found in review (#45).

A replay change must agree with its replay's provenance. A live update
needs a Match that counts, and a redelivered live update writes nothing.
Applies for one player serialize on the Player row. The database refuses
values that are not finite, ``record_replay`` refuses them cleanly, a
replay can carry a chosen spec version (#48), and rating states are never
bulk inserted. Made-up data only.
"""

import math
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from apps.audit.models import AuditAction, AuditEvent
from apps.matches.models import (
    FlagReason,
    Game,
    Match,
    MatchFlag,
    MatchParticipant,
    MatchStatus,
    Team,
)
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.engine_adapter import RatingSummary
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
    RatingStateWriteRefused,
)
from apps.ratings.services import RatingApplyError, apply_rating, record_replay
from apps.ratings.tests.factories import (
    make_history,
    make_state,
    rating_summary,
    replay_provenance,
    state_values,
)

pytestmark = pytest.mark.django_db

REPLAY_DATE = date(2026, 3, 31)


def _counted_match(player: Player) -> Match:
    """A confirmed singles Match that counts, with ``player`` on team A."""
    match: Match = MatchFactory.create(
        status=MatchStatus.CONFIRMED, participants=False, games=False
    )
    MatchParticipant.objects.create(match=match, player=player, team=Team.A)
    MatchParticipant.objects.create(
        match=match, player=PlayerFactory.create(), team=Team.B
    )
    Game.objects.create(match=match, game_number=1, team_a_score=11, team_b_score=6)
    assert match.is_counted()
    return match


@pytest.fixture
def player() -> Player:
    made: Player = PlayerFactory.create()
    return made


def _singles(player: Player, **overrides: Any) -> RatingSummary:
    return rating_summary(player, rating_format="singles", **overrides)


def _events() -> int:
    return AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE).count()


def _nothing_written() -> None:
    assert not RatingState.objects.exists()
    assert not RatingHistory.objects.exists()
    assert _events() == 0


# 1. A replay change agrees with its replay


@pytest.mark.parametrize(
    "overrides",
    [
        {"calculated_as_of": None},
        {"calculated_as_of": date(2025, 1, 1)},
        {"live_updates_since_recalculation": 4},
        {"config_version": "0.9.0"},
        {"params_version": None},
        {"parameter_fingerprint": "ffff"},
    ],
    ids=[
        "never-replayed",
        "other-date",
        "live-updates",
        "config-version",
        "params-version",
        "fingerprint",
    ],
)
def test_a_replay_change_that_disagrees_with_its_replay_is_refused(
    player: Player, overrides: dict[str, Any]
) -> None:
    replay = record_replay(replay_provenance(as_of=REPLAY_DATE))
    summary = _singles(player, **{"calculated_as_of": REPLAY_DATE, **overrides})

    with pytest.raises(RatingApplyError) as caught:
        apply_rating(
            player=player, summary=summary, kind=RatingChangeKind.REPLAY, replay=replay
        )

    _nothing_written()
    assert str(player.public_code) not in str(caught.value)


def test_a_replay_change_that_agrees_with_its_replay_is_stored(
    player: Player,
) -> None:
    replay = record_replay(replay_provenance(as_of=REPLAY_DATE))

    history = apply_rating(
        player=player,
        summary=_singles(player, calculated_as_of=REPLAY_DATE),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
    )

    assert history is not None
    assert history.replay == replay


# 2. A live update needs a Match that counts


@pytest.mark.parametrize(
    "status", [MatchStatus.PENDING, MatchStatus.DISPUTED, MatchStatus.VOIDED]
)
def test_a_live_update_for_a_match_that_does_not_count_is_refused(
    status: str,
) -> None:
    match: Match = MatchFactory.create(status=status)
    participant = match.participants.first()
    assert participant is not None
    player = participant.player

    with pytest.raises(RatingApplyError):
        apply_rating(
            player=player,
            summary=_singles(player),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
        )

    _nothing_written()


def test_a_live_update_for_a_held_match_is_refused(player: Player) -> None:
    held = _counted_match(player)
    flagged: Match = MatchFactory.create()
    MatchFlag.objects.create(
        match=flagged, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=held
    )
    assert not held.is_counted()

    with pytest.raises(RatingApplyError):
        apply_rating(
            player=player,
            summary=_singles(player),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=held,
        )

    _nothing_written()


# 3. Applies for one player serialize on the Player row


def test_apply_locks_the_player_row_before_reading_the_state(
    player: Player,
) -> None:
    match = _counted_match(player)

    with CaptureQueriesContext(connection) as captured:
        apply_rating(
            player=player,
            summary=_singles(player),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
        )

    sql = [query["sql"] for query in captured.captured_queries]
    player_locks = [
        index
        for index, text in enumerate(sql)
        if "FOR UPDATE" in text and 'FROM "players_player"' in text
    ]
    state_reads = [
        index for index, text in enumerate(sql) if 'FROM "ratings_ratingstate"' in text
    ]
    assert player_locks, "no FOR UPDATE on the player row"
    assert state_reads
    assert player_locks[0] < state_reads[0]


# 4. A redelivered live update writes nothing


def test_a_redelivered_live_update_does_not_revert_the_state(
    player: Player,
) -> None:
    first_match, second_match = _counted_match(player), _counted_match(player)
    first, second = _singles(player, rating=3.5), _singles(player, rating=3.6)
    for match, summary in ((first_match, first), (second_match, second)):
        assert (
            apply_rating(
                player=player,
                summary=summary,
                kind=RatingChangeKind.LIVE_UPDATE,
                match=match,
            )
            is not None
        )

    again = apply_rating(
        player=player,
        summary=first,
        kind=RatingChangeKind.LIVE_UPDATE,
        match=first_match,
    )

    assert again is None
    assert RatingState.objects.get(player=player).rating == 3.6
    assert RatingHistory.objects.filter(match=first_match).count() == 1
    assert RatingHistory.objects.count() == 2
    assert _events() == 2


def test_a_redelivered_live_update_after_a_version_change_writes_nothing(
    player: Player,
) -> None:
    match = _counted_match(player)
    summary = _singles(player)
    apply_rating(
        player=player, summary=summary, kind=RatingChangeKind.LIVE_UPDATE, match=match
    )

    with override_settings(OPEN_RATING_SPEC_VERSION="spec-retry-test"):
        again = apply_rating(
            player=player,
            summary=_singles(player, rating=3.9),
            kind=RatingChangeKind.LIVE_UPDATE,
            match=match,
        )

    assert again is None
    assert RatingHistory.objects.count() == 1
    assert RatingState.objects.get(player=player).spec_version == "unpublished"


def test_the_database_refuses_a_second_live_update_row_for_one_match(
    player: Player,
) -> None:
    match = _counted_match(player)
    make_history(player, kind=RatingChangeKind.LIVE_UPDATE, match=match)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_history(player, kind=RatingChangeKind.LIVE_UPDATE, match=match)


# 5. Values that are not finite never reach the database

NOT_FINITE = [
    ("rd", math.nan),
    ("rd", math.inf),
    ("rating", math.nan),
    ("rating", math.inf),
    ("rating", -math.inf),
]


@pytest.mark.parametrize(("field", "value"), NOT_FINITE)
def test_the_database_refuses_a_state_value_that_is_not_finite(
    player: Player, field: str, value: float
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        make_state(player, **{field: value})


@pytest.mark.parametrize(("field", "value"), NOT_FINITE)
def test_the_database_refuses_a_history_value_that_is_not_finite(
    player: Player, field: str, value: float
) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        make_history(player, **{field: value})


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_the_database_refuses_a_delta_that_is_not_finite(
    player: Player, value: float
) -> None:
    match = _counted_match(player)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_history(
            player, kind=RatingChangeKind.LIVE_UPDATE, match=match, delta=value
        )


@pytest.mark.parametrize(
    "net",
    [
        {"singles": math.nan},
        {"singles": math.inf},
        {"bogus": 1.0},
        {"singles": "1.0"},
        {"singles": True},
    ],
    ids=["nan", "inf", "unknown-format", "text", "bool"],
)
def test_record_replay_refuses_bad_net_rating_created(net: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        record_replay(replay_provenance(net_rating_created=net))

    assert "net_rating_created" in caught.value.message_dict
    assert "bogus" not in " ".join(caught.value.messages)
    assert not RatingReplay.objects.exists()


def test_record_replay_accepts_an_empty_net_rating_created() -> None:
    replay = record_replay(replay_provenance(net_rating_created={}))

    replay.refresh_from_db()
    assert replay.net_rating_created == {}


# 6. A replay may carry a chosen spec version (#48)


def test_record_replay_takes_a_spec_version(player: Player) -> None:
    replay = record_replay(replay_provenance(), spec_version="spec-48-test")

    history = apply_rating(
        player=player,
        summary=_singles(player, calculated_as_of=REPLAY_DATE),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
    )

    assert replay.spec_version == "spec-48-test"
    assert history is not None
    assert history.spec_version == "spec-48-test"


def test_record_replay_defaults_to_the_setting() -> None:
    with override_settings(OPEN_RATING_SPEC_VERSION="spec-default-test"):
        replay = record_replay(replay_provenance())

    assert replay.spec_version == "spec-default-test"


# 7. Rating states are never bulk inserted


@pytest.mark.parametrize("manager", ["_default_manager", "_base_manager"])
def test_bulk_inserting_states_is_refused(player: Player, manager: str) -> None:
    with pytest.raises(RatingStateWriteRefused):
        getattr(RatingState, manager).bulk_create(
            [RatingState(player=player, **state_values())]
        )

    assert not RatingState.objects.exists()
