"""The initial in a minor's starting display name is visible (#221).

"First L." takes the last name's first character that renders, with any
combining marks after it, so a direction mark or a decomposed accent in
pasted text does not give an invisible or broken initial (ADR 0022).
Made-up names only.
"""

import pytest

from apps.players.api import starting_display_name
from apps.players.signup import SignupDetails


def _minor(last_name: str) -> SignupDetails:
    return SignupDetails(
        birth_year=2000,
        birth_month=1,
        gender_division="womens",
        legal_name=f"Ann {last_name}",
        city="",
        region="",
        first_name="Ann",
    )


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
    assert starting_display_name(_minor(last_name), minor=True) == display_name


def test_an_adult_starts_with_the_legal_name() -> None:
    details = _minor("‎Smith")

    assert starting_display_name(details, minor=False) == details.legal_name
