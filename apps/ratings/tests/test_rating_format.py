"""The engine gets each Match's stored rating format (#44, ADR 0024).

The adapter maps ``Match.rating_format`` through a table and never reads
the players' current gender divisions, so a division removed after the
match (#190, #116) does not move it to another pool. Made-up data only.
"""

import open_engine
import pytest
from django.db.models import Prefetch

from apps.matches.models import (
    Match,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
    RatingFormat,
)
from apps.matches.tests.factories import DoublesMatchFactory, MatchFactory, raw_update
from apps.players.models import Player
from apps.ratings.engine_adapter import (
    EngineMappingError,
    counted_engine_matches,
    load_config,
    to_engine_match,
)

pytestmark = pytest.mark.django_db


def _stored(rating_format: str) -> Match:
    factory = (
        MatchFactory if rating_format == RatingFormat.SINGLES else DoublesMatchFactory
    )
    made: Match = factory.create(rating_format=rating_format)
    raw_update(made, status=MatchStatus.CONFIRMED)
    return Match.objects.get(pk=made.pk)


def _prefetched(match: Match) -> Match:
    return Match.objects.prefetch_related(
        Prefetch(
            "participants", queryset=MatchParticipant.objects.select_related("player")
        ),
        "games",
        "confirmations",
    ).get(pk=match.pk)


@pytest.mark.parametrize("rating_format", RatingFormat.values)
def test_the_stored_rating_format_is_the_engine_rating_format(
    rating_format: str,
) -> None:
    match = _stored(rating_format)

    converted = to_engine_match(match)

    assert converted.rating_format is open_engine.RatingFormat(rating_format)
    converted.validate()


def test_the_factory_keeps_a_rating_format_it_is_given() -> None:
    assert _stored(RatingFormat.DOUBLES_WOMEN).rating_format == (
        RatingFormat.DOUBLES_WOMEN
    )


def test_a_division_removed_after_the_match_does_not_change_it() -> None:
    match = _stored(RatingFormat.MIXED_DOUBLES)
    before = to_engine_match(match)

    player_ids = match.participants.values_list("player_id", flat=True)
    Player.objects.filter(pk__in=list(player_ids)).update(gender_division="")

    after = to_engine_match(Match.objects.get(pk=match.pk))
    assert after == before
    assert after.rating_format is open_engine.RatingFormat.MIXED_DOUBLES
    [listed] = counted_engine_matches()
    assert listed.rating_format is open_engine.RatingFormat.MIXED_DOUBLES


def test_a_division_changed_in_memory_is_not_read() -> None:
    match = _prefetched(_stored(RatingFormat.DOUBLES_MEN))
    for participant in match.participants.all():
        participant.player.gender_division = "made_up_value"

    converted = to_engine_match(match)

    assert converted.rating_format is open_engine.RatingFormat.DOUBLES_MEN


def test_the_engine_records_each_category_in_its_pool() -> None:
    engine = open_engine.RatingEngine(load_config())
    for rating_format in (
        RatingFormat.DOUBLES_MEN,
        RatingFormat.DOUBLES_WOMEN,
        RatingFormat.MIXED_DOUBLES,
    ):
        match = _stored(rating_format)
        result = engine.record(to_engine_match(match))
        assert result.rating_format is open_engine.RatingFormat(rating_format)


def test_an_unknown_rating_format_raises() -> None:
    match = _stored(RatingFormat.DOUBLES)
    match.rating_format = "triples_mixed"

    with pytest.raises(EngineMappingError, match="rating format"):
        to_engine_match(match)


@pytest.mark.parametrize(
    ("match_format", "rating_format"),
    [
        (MatchFormat.SINGLES, RatingFormat.DOUBLES_MEN),
        (MatchFormat.DOUBLES, RatingFormat.SINGLES),
    ],
)
def test_a_rating_format_that_does_not_fit_the_format_raises(
    match_format: str, rating_format: str
) -> None:
    match = _stored(RatingFormat.DOUBLES)
    match.format = match_format
    match.rating_format = rating_format

    with pytest.raises(EngineMappingError, match="rating format"):
        to_engine_match(match)
