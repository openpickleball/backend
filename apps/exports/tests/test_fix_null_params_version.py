"""A rating with no published parameter set still exports (#39).

``RatingState.params_version`` and ``RatingHistory.params_version`` are
NULL when the config is not a published set, but the export typed them
as text, so every build for that player failed. Made-up data only.
"""

import json

import pytest

from apps.exports.builder import build_export
from apps.players.tests.factories import PlayerFactory
from apps.ratings.models import RatingChangeKind
from apps.ratings.tests.factories import make_history, make_state

pytestmark = pytest.mark.django_db


def test_a_null_params_version_is_exported_as_null() -> None:
    player = PlayerFactory.create()
    make_state(player, rating_format="singles", params_version=None)
    make_history(
        player,
        rating_format="singles",
        kind=RatingChangeKind.REPLAY,
        params_version=None,
    )

    data = json.loads(build_export(player.account).model_dump_json())

    assert data["ratings"]["singles"]["params_version"] is None
    (row,) = data["rating_history"]
    assert row["params_version"] is None
