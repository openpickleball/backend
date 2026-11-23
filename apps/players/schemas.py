"""The API's request and response schemas for Players (issue #124).

No schema here carries a row id. Another Account's view of a player
(``PlayerProfileOut``) holds only the OPEN Code, the display name, and the
city and region when the player's privacy settings let the viewer see
them: never the legal name, the birth year or month, or the gender
division (ADR 0009).
"""

from typing import TYPE_CHECKING

from ninja import Schema

from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User


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
