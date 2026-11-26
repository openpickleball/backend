"""Each Match stores its rating format at creation (#44, ADR 0024).

The rating format comes from the players' gender divisions when the match
is recorded, and never changes afterward, so a later removal of a
division (#190, #116) does not move past matches. A check constraint ties
it to the format. Made-up data only.
"""

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.matches.models import Match, MatchFormat, MatchStatus, RatingFormat
from apps.matches.services import derive_rating_format, record_match
from apps.matches.tests.factories import DoublesMatchFactory, MatchFactory
from apps.matches.workflow import edit_match
from apps.players.models import GenderDivision, Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

MENS = GenderDivision.MENS
WOMENS = GenderDivision.WOMENS
PLAYED_ON = date(2026, 3, 1)


@pytest.mark.parametrize(
    ("match_format", "team_a", "team_b", "expected"),
    [
        (MatchFormat.SINGLES, [MENS], [WOMENS], RatingFormat.SINGLES),
        (MatchFormat.SINGLES, [""], [MENS], RatingFormat.SINGLES),
        (MatchFormat.DOUBLES, [MENS, MENS], [MENS, MENS], RatingFormat.DOUBLES_MEN),
        (
            MatchFormat.DOUBLES,
            [WOMENS, WOMENS],
            [WOMENS, WOMENS],
            RatingFormat.DOUBLES_WOMEN,
        ),
        (
            MatchFormat.DOUBLES,
            [MENS, WOMENS],
            [WOMENS, MENS],
            RatingFormat.MIXED_DOUBLES,
        ),
        (MatchFormat.DOUBLES, [MENS, MENS], [WOMENS, WOMENS], RatingFormat.DOUBLES),
        (MatchFormat.DOUBLES, [MENS, WOMENS], [MENS, MENS], RatingFormat.DOUBLES),
        (MatchFormat.DOUBLES, [MENS, WOMENS], [WOMENS, WOMENS], RatingFormat.DOUBLES),
        (MatchFormat.DOUBLES, [MENS, MENS], [MENS, ""], RatingFormat.DOUBLES),
        (MatchFormat.DOUBLES, [MENS, ""], [WOMENS, MENS], RatingFormat.DOUBLES),
    ],
)
def test_derive_rating_format(
    match_format: str, team_a: list[str], team_b: list[str], expected: RatingFormat
) -> None:
    assert derive_rating_format(match_format, team_a, team_b) == expected


def test_derive_refuses_an_unknown_division_without_its_value() -> None:
    with pytest.raises(ValueError, match="gender division") as raised:
        derive_rating_format(MatchFormat.DOUBLES, [MENS, "made_up_value"], [MENS, MENS])

    assert "made_up_value" not in str(raised.value)


def test_derive_refuses_an_unknown_format() -> None:
    with pytest.raises(ValueError, match="format"):
        derive_rating_format("triples", [MENS], [MENS])


def test_the_values_are_the_engine_rating_format_values() -> None:
    assert set(RatingFormat.values) == {
        "singles",
        "doubles",
        "mixed_doubles",
        "doubles_men",
        "doubles_women",
    }


def _players(*divisions: str) -> list[Player]:
    return [PlayerFactory.create(gender_division=d) for d in divisions]


def _record(players: list[Player], match_format: str = MatchFormat.DOUBLES) -> Match:
    half = len(players) // 2
    return record_match(
        format=match_format,
        team_a=players[:half],
        team_b=players[half:],
        games=[(11, 4)],
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


@pytest.mark.parametrize(
    ("divisions", "expected"),
    [
        ((MENS, MENS, MENS, MENS), RatingFormat.DOUBLES_MEN),
        ((WOMENS, WOMENS, WOMENS, WOMENS), RatingFormat.DOUBLES_WOMEN),
        ((MENS, WOMENS, WOMENS, MENS), RatingFormat.MIXED_DOUBLES),
        ((MENS, MENS, WOMENS, WOMENS), RatingFormat.DOUBLES),
    ],
)
def test_record_match_stores_the_rating_format(
    divisions: tuple[str, ...], expected: RatingFormat
) -> None:
    match = _record(_players(*divisions))

    match.refresh_from_db()
    assert match.rating_format == expected


def test_record_match_stores_singles() -> None:
    match = _record(_players(MENS, WOMENS), MatchFormat.SINGLES)

    match.refresh_from_db()
    assert match.rating_format == RatingFormat.SINGLES


def test_record_match_reads_the_divisions_from_the_database() -> None:
    players = _players(MENS, MENS, MENS, MENS)
    # A stale instance: the stored division is the one that counts.
    Player.objects.filter(pk=players[0].pk).update(gender_division=WOMENS)

    match = _record(players)

    match.refresh_from_db()
    assert match.rating_format == RatingFormat.DOUBLES


def test_a_later_division_removal_keeps_the_stored_value() -> None:
    players = _players(MENS, WOMENS, WOMENS, MENS)
    match = _record(players)

    Player.objects.filter(pk=players[1].pk).update(gender_division="")

    match.refresh_from_db()
    assert match.rating_format == RatingFormat.MIXED_DOUBLES


def test_a_correction_gets_its_own_rating_format() -> None:
    players = _players(MENS, MENS, MENS, MENS)
    old = _record(players)
    Player.objects.filter(pk=players[0].pk).update(gender_division="")

    # A player's correction of the pending self-submission (#267).
    corrected = edit_match(
        old,
        actor=players[1].account,
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=[(11, 6)],
        played_on=PLAYED_ON,
        as_player=True,
    )

    old.refresh_from_db()
    corrected.refresh_from_db()
    assert old.status == MatchStatus.VOIDED
    assert old.rating_format == RatingFormat.DOUBLES_MEN
    assert corrected.rating_format == RatingFormat.DOUBLES


def test_the_factories_store_a_rating_format_that_fits_the_format() -> None:
    singles = MatchFactory.create()
    doubles = DoublesMatchFactory.create()
    bare = DoublesMatchFactory.create(participants=False, games=False)

    assert singles.rating_format == RatingFormat.SINGLES
    assert doubles.rating_format != RatingFormat.SINGLES
    assert bare.rating_format == RatingFormat.DOUBLES


def test_the_factory_derives_the_rating_format_from_its_players() -> None:
    made = DoublesMatchFactory.create()
    divisions = {
        team: [
            p.player.gender_division for p in made.participants.all() if p.team == team
        ]
        for team in ("A", "B")
    }

    assert made.rating_format == derive_rating_format(
        MatchFormat.DOUBLES, divisions["A"], divisions["B"]
    )


@pytest.mark.parametrize(
    ("match_format", "rating_format"),
    [
        (MatchFormat.SINGLES, RatingFormat.DOUBLES),
        (MatchFormat.SINGLES, RatingFormat.MIXED_DOUBLES),
        (MatchFormat.DOUBLES, RatingFormat.SINGLES),
        (MatchFormat.DOUBLES, "triples"),
        (MatchFormat.SINGLES, ""),
    ],
)
def test_the_database_ties_the_rating_format_to_the_format(
    match_format: str, rating_format: str
) -> None:
    with pytest.raises(IntegrityError) as raised, transaction.atomic():
        MatchFactory.create(
            format=match_format,
            rating_format=rating_format,
            participants=False,
            games=False,
        )

    assert "matches_match_rating_format_fits_format" in str(raised.value)


def test_full_clean_refuses_an_unknown_rating_format_without_its_value() -> None:
    match = MatchFactory.build(rating_format="made_up_value")

    with pytest.raises(ValidationError) as raised:
        match.full_clean(validate_constraints=False)

    assert "rating_format" in raised.value.message_dict
    assert "made_up_value" not in str(raised.value)


def test_full_clean_refuses_a_rating_format_that_does_not_fit_the_format() -> None:
    match = MatchFactory.build(rating_format=RatingFormat.DOUBLES_MEN)

    with pytest.raises(ValidationError) as raised:
        match.full_clean(validate_constraints=False)

    assert "rating_format" in raised.value.message_dict
