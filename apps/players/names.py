"""The display name, built from the first and last name (issue #354, ADR 0062).

A player cannot type a display name. It is built from the first and last
name given at signup and the player's ``name_format`` privacy setting:
"First Last" (``full``) or "First L." (``first_last_initial``). Only pure
functions here, so the data migration that fills the names can use them.
"""

import unicodedata

from apps.players.models import (
    DISPLAY_NAME_MAX_LENGTH,
    NameFormat,
    has_visible_character,
)


def last_initial(last_name: str) -> str:
    """The last name's first character that renders, with its combining marks.

    The marks after it are kept, so an accented initial stays whole. The
    signup form makes sure the last name has a character that renders;
    without one, the first character is used.
    """
    start = next(
        (i for i, char in enumerate(last_name) if has_visible_character(char)), 0
    )
    end = start + 1
    while end < len(last_name) and unicodedata.category(last_name[end]).startswith("M"):
        end += 1
    return last_name[start:end]


def full_name(first_name: str, last_name: str) -> str:
    """The legal name, "First Last", which is also the ``full`` display name."""
    return f"{first_name} {last_name}"


def display_name_for(first_name: str, last_name: str, name_format: str) -> str:
    """The display name ``first_name``, ``last_name``, and ``name_format`` give.

    "First Last" for ``full``. "First L." for ``first_last_initial`` (and
    any other value, so an unknown format fails closed): the first name, a
    space, the last name's initial (``last_initial``), and a period. The
    initial's combining marks are dropped from the end while the name
    would not fit the display name's length; the signup checks keep the
    first name short enough for one character and the period.
    """
    if name_format == NameFormat.FULL:
        return full_name(first_name, last_name)
    initial = last_initial(last_name)
    room = DISPLAY_NAME_MAX_LENGTH - len(first_name) - 2
    return f"{first_name} {initial[: max(room, 1)]}."
