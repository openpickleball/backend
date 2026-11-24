"""Removing a rating the replay no longer produces (#47). Made-up data only.

Only the replay's private removal path deletes a ``RatingState``: it
appends a ``removed`` history row naming the replay, with the values
removed, records one ``rating.change`` Audit Event whose ``after`` is
None, and then deletes the row. ``apply_rating`` refuses the removed
kind, and every public delete path still refuses.
"""

from datetime import date

import pytest
from django.contrib.contenttypes.models import ContentType

from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import snapshot
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import (
    RatingChangeKind,
    RatingHistory,
    RatingReplay,
    RatingState,
    RatingStateWriteRefused,
)
from apps.ratings.services import (
    VALUE_FIELDS,
    RatingApplyError,
    _remove_rating,
    apply_rating,
    record_replay,
)
from apps.ratings.tests.factories import rating_summary, replay_provenance

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 3, 31)


@pytest.fixture
def player() -> Player:
    made: Player = PlayerFactory.create()
    return made


@pytest.fixture
def replay() -> RatingReplay:
    return record_replay(replay_provenance(as_of=AS_OF))


def _rated(player: Player, replay: RatingReplay) -> RatingState:
    apply_rating(
        player=player,
        summary=rating_summary(
            player, calculated_as_of=AS_OF, params_version=replay.params_version
        ),
        kind=RatingChangeKind.REPLAY,
        replay=replay,
    )
    return RatingState.objects.get(player=player)


def test_apply_rating_refuses_the_removed_kind(
    player: Player, replay: RatingReplay
) -> None:
    with pytest.raises(RatingApplyError, match=r"A removal goes through the replay\."):
        apply_rating(
            player=player,
            summary=rating_summary(player, calculated_as_of=AS_OF),
            kind=RatingChangeKind.REMOVED,
            replay=replay,
        )

    assert not RatingState.objects.exists()
    assert not RatingHistory.objects.exists()


def test_removal_appends_a_removed_row_with_the_old_values(
    player: Player, replay: RatingReplay
) -> None:
    state = _rated(player, replay)
    later = record_replay(replay_provenance(as_of=date(2026, 4, 30)))

    row = _remove_rating(player=player, state=state, replay=later)

    assert row.kind == RatingChangeKind.REMOVED
    assert row.replay == later
    assert row.match is None
    assert row.delta is None
    assert row.player == player
    assert row.rating_format == state.rating_format
    for name in VALUE_FIELDS:
        assert getattr(row, name) == getattr(state, name)
    assert RatingHistory.objects.filter(kind=RatingChangeKind.REMOVED).count() == 1


def test_removal_deletes_the_state(player: Player, replay: RatingReplay) -> None:
    state = _rated(player, replay)

    _remove_rating(player=player, state=state, replay=replay)

    assert not RatingState.objects.filter(player=player).exists()


def test_removal_records_one_audit_event_with_no_after(
    player: Player, replay: RatingReplay
) -> None:
    state = _rated(player, replay)
    state_id = state.pk
    before = snapshot(state, VALUE_FIELDS)
    events = AuditEvent.objects.filter(action=AuditAction.RATING_CHANGE)
    count = events.count()

    _remove_rating(player=player, state=state, replay=replay)

    assert events.count() == count + 1
    event = events.order_by("-pk").first()
    assert event is not None
    assert event.before == before
    assert event.after is None
    assert event.actor_user is None
    assert event.target_content_type == ContentType.objects.get_for_model(RatingState)
    assert event.target_object_id == str(state_id)


def test_removal_refuses_a_state_of_another_player(
    player: Player, replay: RatingReplay
) -> None:
    state = _rated(player, replay)
    other: Player = PlayerFactory.create()

    with pytest.raises(RatingApplyError):
        _remove_rating(player=other, state=state, replay=replay)

    assert RatingState.objects.filter(pk=state.pk).exists()
    assert not RatingHistory.objects.filter(kind=RatingChangeKind.REMOVED).exists()


def test_removal_refuses_an_unsaved_replay(
    player: Player, replay: RatingReplay
) -> None:
    state = _rated(player, replay)

    with pytest.raises(RatingApplyError):
        _remove_rating(player=player, state=state, replay=RatingReplay())

    assert RatingState.objects.filter(pk=state.pk).exists()


def test_the_public_delete_paths_still_refuse(
    player: Player, replay: RatingReplay
) -> None:
    state = _rated(player, replay)

    with pytest.raises(RatingStateWriteRefused):
        state.delete()
    with pytest.raises(RatingStateWriteRefused):
        RatingState.objects.filter(pk=state.pk).delete()
    with pytest.raises(RatingStateWriteRefused):
        RatingState._base_manager.filter(pk=state.pk).delete()

    assert RatingState.objects.filter(pk=state.pk).exists()
