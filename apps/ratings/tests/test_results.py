"""The engine config, engine results as plain data, and counted matches (#44).

``load_config`` loads the published parameter set the settings name.
``rating_update`` and ``replay_provenance`` turn engine results into
frozen dataclasses of plain values, so no engine enum or object leaves
the adapter. ``counted_engine_matches`` converts exactly the matches that
count. Made-up data only.
"""

import dataclasses
from datetime import date

import open_engine
import pytest
from django.conf import settings
from django.test import override_settings
from pytest_django import DjangoAssertNumQueries

from apps.events.models import EventKind
from apps.events.tests.factories import EventFactory
from apps.matches.models import FlagReason, MatchFlag, MatchStatus, Team
from apps.players.models import GenderDivision
from apps.ratings.engine_adapter import (
    RatingUpdate,
    ReplayProvenance,
    counted_engine_matches,
    engine_version,
    load_config,
    rating_update,
    replay_provenance,
    to_engine_match,
)
from apps.ratings.tests.builders import saved_match

PLAIN_LEAVES = (str, int, float, bool, type(None), date)


def _assert_plain(value: object) -> None:
    """Every value is exactly a plain type, a tuple or dict of them, or a dataclass of them."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        assert type(value) in (RatingUpdate, ReplayProvenance)
        for field in dataclasses.fields(value):
            _assert_plain(getattr(value, field.name))
    elif type(value) is tuple:
        for item in value:
            _assert_plain(item)
    elif type(value) is dict:
        for key, item in value.items():
            _assert_plain(key)
            _assert_plain(item)
    else:
        assert type(value) in PLAIN_LEAVES, type(value)


# Config


def test_the_parameter_set_is_v1() -> None:
    assert settings.OPEN_ENGINE_PARAMETER_SET == "v1"


def test_load_config_is_the_published_parameter_set() -> None:
    config = load_config()

    assert type(config) is open_engine.Config
    assert config == open_engine.Config.published("v1")
    assert config.params_version == "v1"


@override_settings(OPEN_ENGINE_PARAMETER_SET="v999")
def test_an_unknown_parameter_set_raises() -> None:
    with pytest.raises(open_engine.ConfigError):
        load_config()


# Rating updates


@pytest.mark.django_db
def test_a_counted_update_converts_to_plain_data() -> None:
    match, players = saved_match(
        event=EventFactory.create(kind=EventKind.TOURNAMENT),
        divisions={
            Team.A: (GenderDivision.MENS, GenderDivision.MENS),
            Team.B: (GenderDivision.MENS, GenderDivision.MENS),
        },
    )
    result = open_engine.RatingEngine(load_config()).record(to_engine_match(match))

    update = rating_update(result)

    _assert_plain(update)
    assert dataclasses.is_dataclass(update)
    assert update.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert update.counted is True
    assert update.rating_format == "doubles_men"
    assert update.reason is None
    assert update.expected_a == result.expected_a
    assert update.actual_a == result.actual_a
    assert update.surprise == result.surprise
    assert update.effective_weight == result.effective_weight
    assert update.deltas == result.deltas
    assert set(update.deltas) == {
        str(p.public_code) for team in players.values() for p in team
    }
    assert update.frozen == result.frozen
    assert update.clamped == result.clamped
    assert update.imbalance == result.imbalance
    assert update.late is result.late
    assert update.possible_duplicate_of is None
    assert result.parent is not None
    assert update.parent is not None
    assert update.parent.rating_format == "doubles"
    assert update.parent.deltas == result.parent.deltas
    assert update.parent.parent is None


@pytest.mark.django_db
def test_a_rejected_update_names_its_reason_as_a_plain_str() -> None:
    match, _ = saved_match(confirm=[(Team.A, 0)])
    engine = open_engine.RatingEngine(load_config())
    engine.record(to_engine_match(match))

    # The same match id again is rejected as a duplicate.
    update = rating_update(engine.record(to_engine_match(match)))

    _assert_plain(update)
    assert update.counted is False
    assert update.reason == "duplicate_match_id"
    assert type(update.reason) is str
    assert update.parent is None


@pytest.mark.django_db
def test_a_singles_update_has_no_parent() -> None:
    match, _ = saved_match(
        match_format="singles",
        divisions={Team.A: (GenderDivision.WOMENS,), Team.B: (GenderDivision.MENS,)},
        games=((11, 7),),
        best_of=1,
        event=EventFactory.create(),
    )

    update = rating_update(
        open_engine.RatingEngine(load_config()).record(to_engine_match(match))
    )

    _assert_plain(update)
    assert update.rating_format == "singles"
    assert update.parent is None


# Replay provenance


@pytest.mark.django_db
def test_replay_provenance_converts_to_plain_data() -> None:
    match, _ = saved_match(event=EventFactory.create(kind=EventKind.LEAGUE))
    config = load_config()
    replayed = open_engine.replay(
        [to_engine_match(match)], cfg=config, as_of=date(2026, 3, 31)
    )
    info = replayed.replay_info
    assert info is not None

    provenance = replay_provenance(info)

    _assert_plain(provenance)
    assert provenance.__dataclass_params__.frozen  # type: ignore[attr-defined]
    assert provenance.as_of == date(2026, 3, 31)
    assert provenance.config_version == config.version
    assert provenance.config_fingerprint == config.fingerprint()
    assert provenance.params_version == "v1"
    assert provenance.parameter_fingerprint == config.parameter_fingerprint()
    assert provenance.matches_considered == 1
    assert provenance.matches_counted == 1
    assert provenance.net_rating_created == {
        str(fmt.value): value for fmt, value in info.net_rating_created.items()
    }
    assert set(provenance.net_rating_created) == {
        fmt.value for fmt in open_engine.RatingFormat
    }
    assert provenance.engine_version == engine_version()


# Counted matches


@pytest.mark.django_db
def test_counted_engine_matches_converts_only_counted_matches(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    counted = [saved_match(event=EventFactory.create())[0] for _ in range(3)]
    for status in (MatchStatus.PENDING, MatchStatus.DISPUTED, MatchStatus.VOIDED):
        saved_match(status=status)
    held, _ = saved_match()
    held_by, _ = saved_match(status=MatchStatus.PENDING)
    MatchFlag.objects.create(
        match=held_by, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=held
    )

    with django_assert_num_queries(4):
        converted = counted_engine_matches()

    assert sorted(m.match_id for m in converted) == sorted(
        str(m.public_code) for m in counted
    )
    assert all(type(m) is open_engine.Match for m in converted)
    for m in converted:
        m.validate()


@pytest.mark.django_db
def test_counted_engine_matches_uses_the_same_queries_for_more_matches(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    for _ in range(6):
        saved_match(event=EventFactory.create(), confirm=[(Team.A, 0), (Team.B, 1)])

    with django_assert_num_queries(4):
        converted = counted_engine_matches()

    assert len(converted) == 6
    assert all(len(m.confirmed_by) == 2 for m in converted)


@pytest.mark.django_db
def test_counted_engine_matches_is_empty_without_counted_matches(
    django_assert_num_queries: DjangoAssertNumQueries,
) -> None:
    saved_match(status=MatchStatus.PENDING)

    with django_assert_num_queries(1):
        assert counted_engine_matches() == []
