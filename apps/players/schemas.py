"""The API's request and response schemas for Players (issue #124).

The signed-in Account's own Player (``MyPlayerOut``) shows every profile
field and privacy setting, to that Account only. No schema here carries a
row id. Another Account's view of a player
(``PlayerProfileOut``) holds only the OPEN Code, the display name, and the
city and region when the player's privacy settings let the viewer see
them: never the legal name, the birth year or month, or the gender
division (ADR 0009).
"""

from typing import TYPE_CHECKING, Annotated

from ninja import Field, Schema
from pydantic import BeforeValidator, ConfigDict, StrictInt
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from apps.players.models import (
    SET_ONCE_CODE,
    SET_ONCE_MESSAGES,
    NameFormat,
    Player,
    PlayerPrivacy,
    Visibility,
)
from apps.players.services import is_name_format_locked

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


class PlayerCreateIn(Schema):
    """A new Player for the signed-in Account.

    ``birth_year`` and ``birth_month`` take JSON integers only: a boolean,
    a numeric string, or a float is refused, as ``Player.clean_fields``
    refuses them.

    The body is read only when the session holds no details from signup
    (ADR 0017, ADR 0019, ADR 0022): ``first_name``, ``last_name``,
    ``birth_year``, ``birth_month``, and ``gender_division`` are then
    required, and the names get the same checks as at signup. Held details
    are used instead of every field here. Either way the legal name is
    "First Last", and the display name is built from the names: "First
    Last" for an adult, "First L." for a player aged 13 to 17 (ADR 0062).
    ``gender_division`` is ``mens`` or ``womens``. Optional text fields
    left out or null are empty. Unknown keys are refused, ``display_name``
    and ``legal_name`` included.
    """

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = None
    last_name: str | None = None
    birth_year: StrictInt | None = None
    birth_month: StrictInt | None = None
    gender_division: str | None = None
    city: str | None = None
    region: str | None = None


_RATING_VISIBILITY = (
    "Who may see the player's current ratings. A player who joined at 13 to "
    "17 starts with private and keeps it at 18 until they change it; only "
    "the player can change it to signed_in, and only from age 18. An adult's "
    "is always signed_in: current ratings cannot be hidden. Sending the "
    "stored value is always accepted."
)


_NAME_FORMAT = (
    'How the display name shows the last name: full ("First Last") or '
    'first_last_initial ("First L."), built from the first and last name '
    "given at signup. An adult starts with full, a player aged 13 to 17 with "
    "first_last_initial. Changing it rebuilds the display name. Only the "
    "player can change it, and not to full while name_format_locked is true "
    "(the player is 13 to 17). Nothing changes it at 18. Sending the stored "
    "value is always accepted."
)


class PrivacyIn(Schema):
    """Privacy settings to change. Only the settings sent change."""

    model_config = ConfigDict(extra="forbid")

    city_region_visibility: Visibility | None = None
    match_history_visibility: Visibility | None = None
    rating_history_visibility: Visibility | None = None
    stats_visibility: Visibility | None = None
    home_club_visibility: Visibility | None = None
    rating_visibility: Visibility | None = Field(None, description=_RATING_VISIBILITY)
    name_format: NameFormat | None = Field(None, description=_NAME_FORMAT)


def _refuse_set_once(name: str) -> BeforeValidator:
    """Refuse any value sent for ``name``, a field set once, at create.

    Null and the stored value included, with a fixed 422 on that field
    (ADR 0023).
    """

    def refuse(value: object) -> object:
        raise PydanticCustomError(SET_ONCE_CODE, SET_ONCE_MESSAGES[name])

    return BeforeValidator(refuse)


# Left out of the OpenAPI document, since no value is accepted.
SetOnceDisplayName = SkipJsonSchema[Annotated[object, _refuse_set_once("display_name")]]
SetOnceLegalName = SkipJsonSchema[Annotated[object, _refuse_set_once("legal_name")]]
SetOnceBirthYear = SkipJsonSchema[Annotated[object, _refuse_set_once("birth_year")]]
SetOnceBirthMonth = SkipJsonSchema[Annotated[object, _refuse_set_once("birth_month")]]
SetOnceGenderDivision = SkipJsonSchema[
    Annotated[object, _refuse_set_once("gender_division")]
]


class PlayerUpdateIn(Schema):
    """Changes to the signed-in Account's own Player.

    Only the fields sent change. Null clears an optional text field
    (``city``, ``region``). The birth year and month and the gender
    division are set once, when the Player is created, and cannot be sent
    here (ADR 0023). Neither can the display name or the legal name: both
    are built from the first and last name given at signup, and the
    player changes only ``privacy.name_format`` (ADR 0062). ``privacy``
    changes the settings it names. Unknown keys are refused.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: SetOnceDisplayName = None
    legal_name: SetOnceLegalName = None
    birth_year: SetOnceBirthYear = None
    birth_month: SetOnceBirthMonth = None
    gender_division: SetOnceGenderDivision = None
    city: str | None = None
    region: str | None = None
    privacy: PrivacyIn | None = None


class PrivacyOut(Schema):
    """Who may see each of the player's other profile details."""

    city_region_visibility: Visibility
    match_history_visibility: Visibility
    rating_history_visibility: Visibility
    stats_visibility: Visibility
    home_club_visibility: Visibility
    rating_visibility: Visibility = Field(description=_RATING_VISIBILITY)
    name_format: NameFormat = Field(description=_NAME_FORMAT)


_NAME_FORMAT_LOCKED = (
    "True while the player is 13 to 17: privacy.name_format cannot be changed "
    "to full. Judged at the time of the request, so the app never works out "
    "an age."
)


class MyPlayerOut(Schema):
    """The signed-in Account's own Player, with every profile field.

    Returned only to that Account. Empty optional text fields are ``""``.
    ``gender_division`` is ``""`` only for a Player stored before it was
    required (issue #215). ``first_name`` and ``last_name`` are the names
    given at signup, ``""`` only for a Player stored before them whose
    legal name did not split (ADR 0062).
    """

    code: str
    display_name: str
    legal_name: str
    first_name: str
    last_name: str
    birth_year: int
    birth_month: int
    gender_division: str
    city: str
    region: str
    privacy: PrivacyOut
    name_format_locked: bool = Field(description=_NAME_FORMAT_LOCKED)


def my_player_out(player: Player, privacy: PlayerPrivacy) -> MyPlayerOut:
    """``player`` and its privacy settings, for its own Account."""
    return MyPlayerOut(
        code=player.public_code,
        display_name=player.display_name,
        legal_name=player.legal_name,
        first_name=player.first_name or "",
        last_name=player.last_name or "",
        birth_year=player.birth_year,
        birth_month=player.birth_month,
        gender_division=player.gender_division,
        city=player.city,
        region=player.region,
        privacy=PrivacyOut(
            **{name: getattr(privacy, name) for name in PlayerPrivacy.FIELDS}
        ),
        name_format_locked=is_name_format_locked(player),
    )


class PlayerProfileOut(Schema):
    """A player as another signed-in Account sees them.

    ``city`` and ``region`` are null when the player's
    ``city_region_visibility`` hides them from the viewer, and when they
    are empty, so the answer never tells the two apart.
    """

    code: str
    display_name: str
    city: str | None
    region: str | None


def player_profile_out(player: Player, viewer: "_User") -> PlayerProfileOut:
    """``player`` as ``viewer`` may see it.

    Load ``player`` with its ``privacy`` and ``account``, which
    ``visible_to`` reads.
    """
    shown = player.privacy.visible_to("city_region_visibility", viewer)
    return PlayerProfileOut(
        code=player.public_code,
        display_name=player.display_name,
        city=(player.city or None) if shown else None,
        region=(player.region or None) if shown else None,
    )
