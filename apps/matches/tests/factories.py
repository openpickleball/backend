"""Factories for Matches, their participants, and their games.

Made-up data only, never real player data.
"""

from datetime import date
from typing import Any

from django.db.models import QuerySet
from factory.declarations import LazyAttribute, LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory
from factory.helpers import post_generation
from faker import Faker

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import (
    BestOf,
    Game,
    GameTarget,
    Match,
    MatchFormat,
    MatchParticipant,
    ScoringType,
    Team,
)
from apps.matches.services import derive_rating_format, match_fingerprint
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

_faker = Faker()

# Where the hooks keep the rows they chose until insert_rows saves them.
_PLANNED_PARTICIPANTS = "_factory_planned_participants"
_PLANNED_GAMES = "_factory_planned_games"

PLAYERS_PER_TEAM = {MatchFormat.SINGLES: 1, MatchFormat.DOUBLES: 2}


def _played_on() -> date:
    return _faker.date_between(start_date="-1y", end_date="today")


def _external_id() -> str:
    return f"ext-{_faker.uuid4()}"


def _game_winners(best_of: int) -> list[str]:
    """Winners of each game, ending on the game that decides the match."""
    needed = best_of // 2 + 1
    winner = _faker.random_element([Team.A, Team.B])
    loser = Team.B if winner == Team.A else Team.A
    loser_wins = _faker.random_int(min=0, max=needed - 1)
    # The loser's wins fall anywhere before the deciding game.
    earlier = [winner] * (needed - 1) + [loser] * loser_wins
    _faker.random.shuffle(earlier)
    return [*earlier, winner]


class MatchParticipantFactory(DjangoModelFactory[MatchParticipant]):
    """A new Player on team A of a new Match that has no other players."""

    class Meta:
        model = MatchParticipant

    # factory_boy's declarations carry no type hints.
    match = SubFactory(
        "apps.matches.tests.factories.MatchFactory", games=False, participants=False
    )  # type: ignore[no-untyped-call]
    player = SubFactory(PlayerFactory)  # type: ignore[no-untyped-call]
    team = Team.A


class GameFactory(DjangoModelFactory[Game]):
    """Game 1 of a new Match that has no other games, won 11 by team A."""

    class Meta:
        model = Game

    match = SubFactory(
        "apps.matches.tests.factories.MatchFactory", games=False, participants=False
    )  # type: ignore[no-untyped-call]
    game_number = 1
    team_a_score = GameTarget.ELEVEN
    team_b_score = LazyFunction(lambda: _faker.random_int(min=0, max=9))  # type: ignore[no-untyped-call]


class MatchFactory(DjangoModelFactory[Match]):
    """A pending singles Match an Account submitted, best of 1 to 11, side-out.

    It has the players its format needs and games that decide it: each
    game's winner reaches the target and the loser stays at least 2 behind.
    Its rating format is derived from its players' divisions, as
    ``record_match`` does; with no players it is the format's base value.
    Traits: ``best_of_1``, ``best_of_3``, ``best_of_5``, ``side_out``,
    ``rally``, and ``by_application`` (a Partner Application submitted it,
    with an external ID). Pass ``games=False`` or ``participants=False``
    to leave those out. A match with both gets the fingerprint
    ``record_match`` would give it, so it is found as a duplicate; one
    without either has a blank fingerprint.

    The ledger guard refuses to save an existing Match again (#26), so the
    Match is inserted once, by the last hook, after its players and scores
    are chosen and its fingerprint is known. Its participants and games
    are inserted after it.
    """

    class Meta:
        model = Match
        skip_postgeneration_save = True

    class Params:
        best_of_1 = Trait(best_of=BestOf.ONE)  # type: ignore[no-untyped-call]
        best_of_3 = Trait(best_of=BestOf.THREE)  # type: ignore[no-untyped-call]
        best_of_5 = Trait(best_of=BestOf.FIVE)  # type: ignore[no-untyped-call]
        side_out = Trait(scoring_type=ScoringType.SIDE_OUT)  # type: ignore[no-untyped-call]
        rally = Trait(scoring_type=ScoringType.RALLY)  # type: ignore[no-untyped-call]
        by_application = Trait(  # type: ignore[no-untyped-call]
            submitted_by_user=None,
            submitted_by_application=SubFactory(PartnerApplicationFactory),  # type: ignore[no-untyped-call]
            external_id=LazyFunction(_external_id),  # type: ignore[no-untyped-call]
        )

    format = MatchFormat.SINGLES
    # The base value of the format; insert_rows derives the category from
    # the players' divisions, as record_match does, unless one was passed.
    rating_format = LazyAttribute(lambda match: str(match.format))  # type: ignore[no-untyped-call]
    played_on = LazyFunction(_played_on)  # type: ignore[no-untyped-call]
    best_of = BestOf.ONE
    game_target = GameTarget.ELEVEN
    scoring_type = ScoringType.SIDE_OUT
    submitted_by_user = SubFactory(UserFactory)  # type: ignore[no-untyped-call]

    @classmethod
    def _create(cls, model_class: type[Match], *args: Any, **kwargs: Any) -> Match:
        # Built, not saved: the insert_rows hook saves it once.
        return model_class(*args, **kwargs)

    @post_generation  # type: ignore[untyped-decorator]
    def participants(self: Match, create: bool, extracted: Any, **kwargs: Any) -> None:
        planned: list[tuple[Player, Team]] = []
        setattr(self, _PLANNED_PARTICIPANTS, planned)
        if not create or extracted is False:
            return
        for team in Team:
            for _ in range(PLAYERS_PER_TEAM[MatchFormat(self.format)]):
                planned.append((PlayerFactory.create(), team))

    @post_generation  # type: ignore[untyped-decorator]
    def games(self: Match, create: bool, extracted: Any, **kwargs: Any) -> None:
        planned: list[tuple[int, int]] = []
        setattr(self, _PLANNED_GAMES, planned)
        if not create or extracted is False:
            return
        target = int(self.game_target)
        for winner in _game_winners(int(self.best_of)):
            loser_score = _faker.random_int(min=0, max=target - 2)
            planned.append(
                (target, loser_score) if winner == Team.A else (loser_score, target)
            )

    @post_generation  # type: ignore[untyped-decorator]
    def insert_rows(self: Match, create: bool, extracted: Any, **kwargs: Any) -> None:
        """Fingerprint the match as record_match does, then insert everything.

        Test setup only, not a ledger edit path. Runs after the participant
        and game hooks. A fingerprint passed to the factory is kept. The
        hooks' names differ from the fields', so factory_boy passes those
        values to the model instead of to a hook.
        """
        if not create:
            return
        participants: list[tuple[Player, Team]] = getattr(self, _PLANNED_PARTICIPANTS)
        games: list[tuple[int, int]] = getattr(self, _PLANNED_GAMES)
        if participants and self.rating_format == self.format:
            self.rating_format = derive_rating_format(
                self.format,
                [p.gender_division for p, side in participants if side == Team.A],
                [p.gender_division for p, side in participants if side == Team.B],
            )
        if not self.fingerprint and participants and games:
            teams = {
                team: [player.pk for player, side in participants if side == team]
                for team in Team
            }
            self.fingerprint = match_fingerprint(
                teams[Team.A], teams[Team.B], self.played_on, games
            )
        self.save()
        for player, team in participants:
            MatchParticipant.objects.create(match=self, player=player, team=team)
        for number, (a, b) in enumerate(games, start=1):
            Game.objects.create(
                match=self, game_number=number, team_a_score=a, team_b_score=b
            )


class DoublesMatchFactory(MatchFactory):
    """A MatchFactory match in doubles: two players per team."""

    format = MatchFormat.DOUBLES


def raw_update(match: Match, **values: Any) -> None:
    """Change a Match row as a raw write would, skipping the ledger guard.

    Test setup only: it stands in for a row written outside the workflow
    services (raw SQL, a fixture), which the ORM guard does not cover.
    """
    QuerySet(model=Match).filter(pk=match.pk).update(**values)
