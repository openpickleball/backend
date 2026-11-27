"""The export keeps the Audit Events of a rating a replay removed (#39).

The builder took the player's rating states from the live rows only, so
once a replay removed a state (``_remove_rating`` records its event and
then deletes the row), that state's events, the removal and every
earlier change, dropped out of ``audit_events``. A state's id belongs to
one player for good, and every state starts with an ``apply_rating``
event that names the player's OPEN Code, so the builder finds the
removed states by those events, never another player's. Made-up data
only.
"""

import json
from datetime import date

import pytest

from apps.exports.builder import build_export
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingChangeKind, RatingReplay, RatingState
from apps.ratings.services import _remove_rating, apply_rating, record_replay
from apps.ratings.tests.factories import rating_summary, replay_provenance

pytestmark = pytest.mark.django_db

AS_OF = date(2026, 3, 31)


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


def _rating_events(player: Player) -> list[dict[str, object]]:
    events = json.loads(build_export(player.account).model_dump_json())["audit_events"]
    return [e for e in events if e["target_type"] == "ratings.ratingstate"]


def test_a_removed_ratings_events_stay_in_the_export() -> None:
    player, other = PlayerFactory.create(), PlayerFactory.create()
    replay = record_replay(replay_provenance(as_of=AS_OF))
    state = _rated(player, replay)
    other_state = _rated(other, replay)

    _remove_rating(player=player, state=state, replay=replay)
    _remove_rating(player=other, state=other_state, replay=replay)

    events = _rating_events(player)
    assert [e["action"] for e in events] == ["rating.change", "rating.change"]
    created, removed = events
    assert created["before"] is None
    assert created["after"] is not None
    assert removed["after"] is None
    assert removed["before"] is not None
    assert len(_rating_events(other)) == 2
