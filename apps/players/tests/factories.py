"""Factories for Players. Made-up data only, never real player data."""

from typing import Any

from factory.declarations import LazyFunction, SubFactory, Trait
from factory.django import DjangoModelFactory
from faker import Faker

from apps.accounts.tests.factories import UserFactory
from apps.players.age import age_limit_date
from apps.players.models import GenderDivision, Player
from apps.players.services import create_player

_faker = Faker()


def _first_name() -> str:
    # Short enough that "First Last" fits the display name (ADR 0022).
    return str(_faker.first_name())[:30]


def _last_name() -> str:
    return str(_faker.last_name())[:30]


def _birth_year() -> int:
    # 18 or older on every day of the year, so default privacy never flips
    # to the minor's private default at random (issue #114).
    this_year = age_limit_date().year
    return _faker.random_int(min=this_year - 70, max=this_year - 19)


def _minor_birth_year() -> int:
    # 13 to 17 on every day of the year, whatever the month.
    this_year = age_limit_date().year
    return _faker.random_int(min=this_year - 17, max=this_year - 14)


def _gender_division() -> str:
    # Every new Player has one (issue #215).
    return str(_faker.random_element(GenderDivision.values))


class PlayerFactory(DjangoModelFactory[Player]):
    """A Player created through ``create_player``, so it has its privacy row.

    An adult by default. ``minor=True`` makes a player aged 13 to 17, who
    starts with every privacy setting private.

    The display name and legal name are built from ``first_name`` and
    ``last_name``, as ``create_player`` builds them (ADR 0062). A test may
    still pass ``display_name`` or ``legal_name``: the factory then saves
    that value over the built one, a shortcut for tests only, since no
    player can set either.
    """

    class Meta:
        model = Player

    class Params:
        minor = Trait(birth_year=LazyFunction(_minor_birth_year))  # type: ignore[no-untyped-call]

    # factory_boy's declarations carry no type hints.
    account = SubFactory(UserFactory)  # type: ignore[no-untyped-call]
    first_name = LazyFunction(_first_name)  # type: ignore[no-untyped-call]
    last_name = LazyFunction(_last_name)  # type: ignore[no-untyped-call]
    birth_year = LazyFunction(_birth_year)  # type: ignore[no-untyped-call]
    birth_month = LazyFunction(lambda: _faker.random_int(min=1, max=12))  # type: ignore[no-untyped-call]
    gender_division = LazyFunction(_gender_division)  # type: ignore[no-untyped-call]
    city = LazyFunction(lambda: _faker.city()[:100])  # type: ignore[no-untyped-call]
    region = LazyFunction(lambda: _faker.state()[:100])  # type: ignore[no-untyped-call]

    @classmethod
    def _create(cls, model_class: type[Any], *args: Any, **kwargs: Any) -> Player:
        """A saved Player, created through the service.

        A ``display_name`` or ``legal_name`` passed in is saved over the
        built one afterwards, through ``save()``, so the model's checks
        still run.
        """
        overrides = {
            name: kwargs.pop(name)
            for name in ("display_name", "legal_name")
            if name in kwargs
        }
        player = create_player(**kwargs)
        if overrides:
            for name, value in overrides.items():
                setattr(player, name, value)
            player.save(update_fields=list(overrides))
        return player
