"""The removal kind and the replay request model (#47). Made-up data only.

A replay that no longer produces a player's rating in a format writes a
``removed`` history row naming the replay, and no Match or delta. A
``RatingReplayRequest`` asks for a replay: it names no Player and holds
no personal data.
"""

from collections.abc import Callable
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.matches.signals import MatchChange
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplayRequest,
    ReplayReason,
)
from apps.ratings.tests.factories import make_history, make_replay, state_values

pytestmark = pytest.mark.django_db


@pytest.fixture
def player() -> Player:
    made: Player = PlayerFactory.create()
    return made


def _raw_insert_fails(build: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        build()


def test_a_removed_row_names_its_replay(player: Player) -> None:
    replay = make_replay()

    row = make_history(player, kind=RatingChangeKind.REMOVED, replay=replay)

    assert row.kind == "removed"
    assert row.replay == replay
    assert row.match is None


def test_a_removed_row_passes_full_clean(player: Player) -> None:
    row = RatingHistory(
        player=player,
        kind=RatingChangeKind.REMOVED,
        replay=make_replay(),
        **state_values(),
    )

    row.full_clean()


def test_a_removed_row_without_a_replay_is_refused(player: Player) -> None:
    _raw_insert_fails(
        lambda: make_history(player, kind=RatingChangeKind.REMOVED, replay=None)
    )


def test_a_removed_row_with_a_match_is_refused(player: Player) -> None:
    match = MatchFactory.create()
    replay = make_replay()

    _raw_insert_fails(
        lambda: make_history(
            player, kind=RatingChangeKind.REMOVED, replay=replay, match=match
        )
    )


def test_a_removed_row_has_no_delta(player: Player) -> None:
    replay = make_replay()

    _raw_insert_fails(
        lambda: make_history(
            player, kind=RatingChangeKind.REMOVED, replay=replay, delta=0.5
        )
    )


def test_the_kind_message_does_not_repeat_a_value(player: Player) -> None:
    row = RatingHistory(player=player, kind=RatingChangeKind.REMOVED, **state_values())

    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    text = " ".join(caught.value.messages)
    assert "removed" in text.lower()
    assert str(player.pk) not in text


# Replay requests


def test_the_reasons_are_every_match_change_and_the_live_update_cases() -> None:
    assert set(ReplayReason.values) == {
        *(change.value for change in MatchChange),
        "late",
        "replay_needed",
    }


def test_a_request_stores_its_reason_and_date() -> None:
    request = RatingReplayRequest.objects.create(
        earliest_date=date(2026, 3, 1), reason=ReplayReason.VOID
    )

    request.refresh_from_db()
    assert request.reason == "void"
    assert request.earliest_date == date(2026, 3, 1)
    assert request.created_at is not None
    assert str(request) == "void from 2026-03-01"


def test_a_request_names_no_player() -> None:
    names = {field.name for field in RatingReplayRequest._meta.get_fields()}
    assert names == {"id", "created_at", "earliest_date", "reason"}


def test_the_database_refuses_an_unknown_reason() -> None:
    _raw_insert_fails(
        lambda: RatingReplayRequest.objects.create(
            earliest_date=date(2026, 3, 1), reason="guess"
        )
    )


def test_full_clean_refuses_an_unknown_reason_without_repeating_it() -> None:
    request = RatingReplayRequest(earliest_date=date(2026, 3, 1), reason="guess")

    with pytest.raises(ValidationError) as caught:
        request.full_clean()

    assert "guess" not in " ".join(caught.value.messages)
