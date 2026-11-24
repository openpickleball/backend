"""Rating state, rating history, and replay rows (#45).

One ``RatingState`` per Player and rating format. ``RatingHistory`` and
``RatingReplay`` rows are append-only (hard rule 3), and every history
and replay row names its engine and spec versions (hard rule 4). The
database refuses values out of range, and ``full_clean()`` reports them
without repeating the value. Made-up data only.
"""

import math
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction

from apps.matches.models import RatingFormat
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingHistoryImmutable,
    RatingReplay,
    RatingState,
    RatingStateWriteRefused,
)
from apps.ratings.tests.factories import (
    make_history,
    make_replay,
    make_state,
    replay_values,
    state_values,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def player() -> Player:
    made: Player = PlayerFactory.create()
    return made


def _messages(error: ValidationError) -> str:
    return " ".join(str(message) for message in error.messages)


def _raw_insert_fails(build: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        build()


def _unsaved(model: type[models.Model], player: Player, **overrides: Any) -> Any:
    """An unsaved valid row of ``model`` with ``overrides``."""
    if model is RatingState:
        return RatingState(player=player, **state_values(**overrides))
    values = state_values(kind=RatingChangeKind.REPLAY, replay=make_replay())
    values.update(overrides)
    return RatingHistory(player=player, **values)


# One state per player and format


def test_a_second_state_for_the_same_player_and_format_is_refused(
    player: Player,
) -> None:
    make_state(player)

    _raw_insert_fails(lambda: make_state(player))


def test_full_clean_reports_a_second_state_plainly(player: Player) -> None:
    make_state(player)

    with pytest.raises(ValidationError) as caught:
        RatingState(player=player, **state_values()).full_clean()

    assert "This player already has a rating in this format." in _messages(caught.value)


def test_another_format_for_the_same_player_is_fine(player: Player) -> None:
    make_state(player)

    other = RatingState(
        player=player, **state_values(rating_format=RatingFormat.MIXED_DOUBLES)
    )
    other.full_clean()
    other.save()

    assert RatingState.objects.filter(player=player).count() == 2


# Reliability from 0 to 100

VALUE_MODELS = (RatingState, RatingHistory)
RELIABILITY_FIELDS = ("reliability", "imported_reliability")


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize("field", RELIABILITY_FIELDS)
@pytest.mark.parametrize("value", [-0.1, 100.1])
def test_full_clean_refuses_reliability_out_of_range(
    player: Player, model: type[models.Model], field: str, value: float
) -> None:
    row = _unsaved(model, player, **{field: value})

    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    assert field in caught.value.message_dict
    assert str(value) not in _messages(caught.value)


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize("field", RELIABILITY_FIELDS)
@pytest.mark.parametrize("value", [-0.1, 100.1])
def test_the_database_refuses_reliability_out_of_range(
    player: Player, model: type[models.Model], field: str, value: float
) -> None:
    row = _unsaved(model, player, **{field: value})

    _raw_insert_fails(row.save)


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize("field", RELIABILITY_FIELDS)
@pytest.mark.parametrize("value", [0.0, 100.0])
def test_reliability_at_the_limits_is_accepted(
    player: Player, model: type[models.Model], field: str, value: float
) -> None:
    row = _unsaved(model, player, **{field: value})

    row.full_clean()
    row.save()

    assert model._default_manager.filter(pk=row.pk).exists()


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize("field", ["rating", "rd", "reliability"])
@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_full_clean_refuses_a_value_that_is_not_finite(
    player: Player, model: type[models.Model], field: str, value: float
) -> None:
    row = _unsaved(model, player, **{field: value})

    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    assert field in caught.value.message_dict


@pytest.mark.parametrize("model", VALUE_MODELS)
def test_the_database_refuses_a_negative_rd(
    player: Player, model: type[models.Model]
) -> None:
    row = _unsaved(model, player, rd=-0.01)

    with pytest.raises(ValidationError) as caught:
        row.full_clean()
    assert "rd" in caught.value.message_dict
    _raw_insert_fails(row.save)


@pytest.mark.parametrize("model", VALUE_MODELS)
def test_messages_never_repeat_a_bad_value(
    player: Player, model: type[models.Model]
) -> None:
    row = _unsaved(
        model,
        player,
        rating="not-a-number-xyz",
        rating_format="bogus-format-xyz",
        matches_played="many-xyz",
        last_played="someday-xyz",
    )

    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    assert {"rating", "rating_format", "matches_played", "last_played"} <= set(
        caught.value.message_dict
    )
    assert "xyz" not in _messages(caught.value)


def test_a_player_link_of_the_wrong_type_is_not_repeated() -> None:
    row = RatingState(player_id="player-xyz", **state_values())

    with pytest.raises(ValidationError) as caught:
        row.full_clean()

    assert "player" in caught.value.message_dict
    assert "xyz" not in _messages(caught.value)


@pytest.mark.parametrize("model", VALUE_MODELS)
def test_the_database_refuses_an_unknown_rating_format(
    player: Player, model: type[models.Model]
) -> None:
    row = _unsaved(model, player, rating_format="bogus")

    _raw_insert_fails(row.save)


# Versions are never blank

VERSION_FIELDS = (
    "engine_version",
    "spec_version",
    "config_version",
    "parameter_fingerprint",
)


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize("field", VERSION_FIELDS)
def test_a_blank_version_is_refused(
    player: Player, model: type[models.Model], field: str
) -> None:
    row = _unsaved(model, player, **{field: ""})

    with pytest.raises(ValidationError) as caught:
        row.full_clean()
    assert field in caught.value.message_dict
    _raw_insert_fails(row.save)


@pytest.mark.parametrize("model", VALUE_MODELS)
def test_a_blank_params_version_is_refused_but_none_is_fine(
    player: Player, model: type[models.Model]
) -> None:
    blank = _unsaved(model, player, params_version="")
    with pytest.raises(ValidationError):
        blank.full_clean()
    _raw_insert_fails(blank.save)

    unlabelled = _unsaved(model, player, params_version=None)
    unlabelled.full_clean()
    unlabelled.save()


REPLAY_REQUIRED_TEXT = (
    "config_version",
    "config_fingerprint",
    "parameter_fingerprint",
    "engine_version",
    "spec_version",
)


@pytest.mark.parametrize("field", REPLAY_REQUIRED_TEXT)
def test_a_replay_with_a_blank_version_is_refused(field: str) -> None:
    replay = RatingReplay(**replay_values(**{field: ""}))

    with pytest.raises(ValidationError) as caught:
        replay.full_clean()
    assert field in caught.value.message_dict
    _raw_insert_fails(replay.save)


def test_a_replay_with_a_blank_params_version_is_refused() -> None:
    _raw_insert_fails(lambda: make_replay(params_version=""))
    assert make_replay(params_version=None).params_version is None


def test_a_replay_counts_no_more_matches_than_it_considered() -> None:
    replay = RatingReplay(**replay_values(matches_considered=3, matches_counted=4))

    with pytest.raises(ValidationError):
        replay.full_clean()
    _raw_insert_fails(replay.save)


def test_a_replay_stores_every_provenance_field() -> None:
    replay = make_replay()
    replay.refresh_from_db()

    assert replay_values() == {name: getattr(replay, name) for name in replay_values()}
    assert replay.created_at is not None


# What caused a history row


def test_a_live_update_links_its_match(player: Player) -> None:
    match = MatchFactory.create()

    row = make_history(
        player, kind=RatingChangeKind.LIVE_UPDATE, match=match, delta=0.031
    )

    assert row.match == match
    assert row.replay is None


def test_a_live_update_without_a_match_is_refused(player: Player) -> None:
    _raw_insert_fails(lambda: make_history(player, kind=RatingChangeKind.LIVE_UPDATE))


def test_a_live_update_with_a_replay_is_refused(player: Player) -> None:
    match = MatchFactory.create()
    replay = make_replay()

    _raw_insert_fails(
        lambda: make_history(
            player, kind=RatingChangeKind.LIVE_UPDATE, match=match, replay=replay
        )
    )


def test_a_replay_row_without_a_replay_is_refused(player: Player) -> None:
    _raw_insert_fails(
        lambda: make_history(player, kind=RatingChangeKind.REPLAY, replay=None)
    )


def test_a_replay_row_has_no_delta(player: Player) -> None:
    _raw_insert_fails(lambda: make_history(player, delta=0.5))


def test_an_unknown_kind_is_refused(player: Player) -> None:
    match = MatchFactory.create()

    _raw_insert_fails(lambda: make_history(player, kind="guess", match=match))


def test_full_clean_reports_a_live_update_without_a_match(player: Player) -> None:
    row = RatingHistory(
        player=player, kind=RatingChangeKind.LIVE_UPDATE, **state_values()
    )

    with pytest.raises(ValidationError):
        row.full_clean()


# Append-only history and replays

APPEND_ONLY_MODELS: tuple[type[models.Model], ...] = (RatingHistory, RatingReplay)


def _append_only_row(model: type[models.Model], player: Player) -> models.Model:
    if model is RatingHistory:
        return make_history(player)
    return make_replay()


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
def test_deleting_an_append_only_row_is_refused(
    player: Player, model: type[models.Model]
) -> None:
    row = _append_only_row(model, player)

    with pytest.raises(RatingHistoryImmutable):
        row.delete()

    assert model._default_manager.filter(pk=row.pk).exists()


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
def test_saving_an_append_only_row_again_is_refused(
    player: Player, model: type[models.Model]
) -> None:
    row = _append_only_row(model, player)

    with pytest.raises(RatingHistoryImmutable):
        row.save()
    with pytest.raises(RatingHistoryImmutable):
        row.save(update_fields=["engine_version"])


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
def test_an_existing_row_marked_new_is_refused(
    player: Player, model: type[models.Model]
) -> None:
    row = _append_only_row(model, player)
    row._state.adding = True

    with pytest.raises(RatingHistoryImmutable):
        row.save()


QUERYSET_CALLS: dict[str, Callable[[models.QuerySet[Any]], object]] = {
    "update": lambda queryset: queryset.update(spec_version="changed"),
    "delete": lambda queryset: queryset.delete(),
    "bulk_update": lambda queryset: queryset.bulk_update(
        list(queryset), ["spec_version"]
    ),
}

MANAGERS = ("_default_manager", "_base_manager")


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
@pytest.mark.parametrize("manager", MANAGERS)
@pytest.mark.parametrize("call", sorted(QUERYSET_CALLS))
def test_bulk_changes_to_append_only_rows_are_refused(
    player: Player, model: type[models.Model], manager: str, call: str
) -> None:
    _append_only_row(model, player)
    queryset = getattr(model, manager).all()

    with pytest.raises(RatingHistoryImmutable):
        QUERYSET_CALLS[call](queryset)

    assert getattr(model, manager).exclude(spec_version="changed").count() == 1


@pytest.mark.parametrize("model", APPEND_ONLY_MODELS)
@pytest.mark.parametrize("manager", MANAGERS)
def test_an_upsert_of_append_only_rows_is_refused(
    player: Player, model: type[models.Model], manager: str
) -> None:
    row = _append_only_row(model, player)

    with pytest.raises(RatingHistoryImmutable):
        getattr(model, manager).bulk_create(
            [row],
            update_conflicts=True,
            update_fields=["spec_version"],
            unique_fields=["id"],
        )


# Rating state: saved only by the service, never bulk changed or deleted


def test_an_existing_state_can_be_saved(player: Player) -> None:
    state = make_state(player)

    state.rating = 3.75
    state.save()

    state.refresh_from_db()
    assert state.rating == 3.75


def test_deleting_a_state_is_refused(player: Player) -> None:
    state = make_state(player)

    with pytest.raises(RatingStateWriteRefused):
        state.delete()

    assert RatingState.objects.filter(pk=state.pk).exists()


@pytest.mark.parametrize("manager", MANAGERS)
@pytest.mark.parametrize("call", sorted(QUERYSET_CALLS))
def test_bulk_changes_to_states_are_refused(
    player: Player, manager: str, call: str
) -> None:
    make_state(player)
    queryset = getattr(RatingState, manager).all()

    with pytest.raises(RatingStateWriteRefused):
        QUERYSET_CALLS[call](queryset)

    assert RatingState.objects.exclude(spec_version="changed").count() == 1


@pytest.mark.parametrize("manager", MANAGERS)
def test_an_upsert_of_states_is_refused(player: Player, manager: str) -> None:
    state = make_state(player)

    with pytest.raises(RatingStateWriteRefused):
        getattr(RatingState, manager).bulk_create(
            [state],
            update_conflicts=True,
            update_fields=["rating"],
            unique_fields=["player", "rating_format"],
        )


def test_a_player_with_a_state_cannot_be_deleted(player: Player) -> None:
    make_state(player)

    with pytest.raises(models.ProtectedError):
        player.delete()


# Provisional: holds live updates not yet replayed (hard rule 3)


@pytest.mark.parametrize("model", VALUE_MODELS)
@pytest.mark.parametrize(
    ("calculated_as_of", "live_updates", "provisional"),
    [
        (None, 0, True),
        (date(2026, 3, 31), 0, False),
        (date(2026, 3, 31), 2, True),
    ],
)
def test_provisional(
    player: Player,
    model: type[models.Model],
    calculated_as_of: date | None,
    live_updates: int,
    provisional: bool,
) -> None:
    row = _unsaved(
        model,
        player,
        calculated_as_of=calculated_as_of,
        live_updates_since_recalculation=live_updates,
    )

    assert row.provisional is provisional


# Text shown for a row: the OPEN Code, never a name


def test_a_state_shows_the_player_code_and_format(player: Player) -> None:
    state = make_state(player)

    assert str(state) == f"{player.public_code} doubles"
    assert player.display_name not in str(state)


def test_a_history_row_shows_the_code_format_and_kind(player: Player) -> None:
    row = make_history(player)

    assert str(row) == f"{player.public_code} doubles replay"
    assert player.display_name not in str(row)


def test_a_replay_shows_its_date() -> None:
    assert str(make_replay()) == "Replay as of 2026-03-31"


def test_history_is_ordered_oldest_first(player: Player) -> None:
    first = make_history(player)
    second = make_history(player)

    assert list(RatingHistory.objects.filter(player=player)) == [first, second]
