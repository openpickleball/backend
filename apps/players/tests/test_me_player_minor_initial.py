"""The initial in a minor's starting display name is visible (#221).

"First L." takes the last name's first character that renders, with any
combining marks after it, so a direction mark or a decomposed accent in
pasted text does not give an invisible or broken initial (ADR 0022). The
name format builds it the same way (ADR 0062).
Made-up names only.
"""

import pytest

from apps.players.models import DISPLAY_NAME_MAX_LENGTH, NameFormat
from apps.players.names import display_name_for


@pytest.mark.parametrize(
    ("last_name", "display_name"),
    [
        ("Smith", "Ann S."),
        ("‎Smith", "Ann S."),
        ("​‮Smith", "Ann S."),
        ("Émile", "Ann É."),
        ("Émile", "Ann É."),
        ("́Lee", "Ann L."),
        ("de la Cruz", "Ann d."),
    ],
)
def test_the_initial_is_the_first_visible_character(
    last_name: str, display_name: str
) -> None:
    built = display_name_for("Ann", last_name, NameFormat.FIRST_LAST_INITIAL)

    assert built == display_name


def test_the_full_format_is_first_and_last_name() -> None:
    assert display_name_for("Ann", "Smith", NameFormat.FULL) == "Ann Smith"


def test_an_unknown_format_gives_the_initial() -> None:
    assert display_name_for("Ann", "Smith", "something else") == "Ann S."


def test_combining_marks_are_dropped_to_fit_the_display_name() -> None:
    first = "A" * (DISPLAY_NAME_MAX_LENGTH - 3)
    last = "E" + "\u0301" * 5

    built = display_name_for(first, last, NameFormat.FIRST_LAST_INITIAL)

    assert built == f"{first} E."
    assert len(built) == DISPLAY_NAME_MAX_LENGTH
