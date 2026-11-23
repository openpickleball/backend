"""The API's request and response schemas for Players (issue #124).

The signed-in Account's own Player (``MyPlayerOut``) shows every profile
field and privacy setting, to that Account only. No schema here carries a
row id. Another Account's view of a player
(``PlayerProfileOut``) holds only the OPEN Code, the display name, and the
city and region when the player's privacy settings let the viewer see
them: never the legal name, the birth year or month, or the gender
division (ADR 0009).
"""

from typing import TYPE_CHECKING

from ninja import Schema
from pydantic import ConfigDict

from apps.players.models import Player, PlayerPrivacy, Visibility

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


class PlayerCreateIn(Schema):
    """A new Player for the signed-in Account.

    ``birth_year`` and ``birth_month`` are read only when the session holds
    no age from signup (ADR 0017); a held age is used instead. Optional
    text fields left out or null are empty. ``gender_division`` is
    ``mens``, ``womens``, or empty. Unknown keys are refused.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str
    birth_year: int | None = None
    birth_month: int | None = None
    legal_name: str | None = None
    gender_division: str | None = None
    city: str | None = None
    region: str | None = None


class PrivacyOut(Schema):
    """Who may see each of the player's other profile details."""

    city_region_visibility: Visibility
    match_history_visibility: Visibility
    rating_history_visibility: Visibility
    stats_visibility: Visibility
    home_club_visibility: Visibility


class MyPlayerOut(Schema):
    """The signed-in Account's own Player, with every profile field.

    Returned only to that Account. Empty optional text fields are ``""``.
    """

    code: str
    display_name: str
    legal_name: str
    birth_year: int
    birth_month: int
    gender_division: str
    city: str
    region: str
    privacy: PrivacyOut


def my_player_out(player: Player, privacy: PlayerPrivacy) -> MyPlayerOut:
    """``player`` and its privacy settings, for its own Account."""
    return MyPlayerOut(
        code=player.public_code,
        display_name=player.display_name,
        legal_name=player.legal_name,
        birth_year=player.birth_year,
        birth_month=player.birth_month,
        gender_division=player.gender_division,
        city=player.city,
        region=player.region,
        privacy=PrivacyOut(
            **{name: getattr(privacy, name) for name in PlayerPrivacy.SETTINGS}
        ),
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
