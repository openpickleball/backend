"""``is_adult``: 18 or older on a date, by the end-of-month rule (ADR 0017).

OPEN stores no day of birth, so a player counts as born on the last day
of their birth month, the latest day they could have been born. For a
February birth in a leap year that is the 29th, which a later common
year lacks, so the 18th birthday is then counted from March 1.
"""

from datetime import date

from apps.players.age import is_adult


def test_turns_18_on_the_last_day_of_the_birth_month() -> None:
    assert not is_adult(2008, 6, on=date(2026, 6, 29))
    assert is_adult(2008, 6, on=date(2026, 6, 30))


def test_a_leap_february_birth_turns_18_after_february_ends() -> None:
    # Born by 2008-02-29 at the latest; 2026 has no February 29.
    assert not is_adult(2008, 2, on=date(2026, 2, 27))
    assert not is_adult(2008, 2, on=date(2026, 2, 28))
    assert is_adult(2008, 2, on=date(2026, 3, 1))


def test_a_common_february_birth_turns_18_on_the_28th() -> None:
    assert not is_adult(2007, 2, on=date(2025, 2, 27))
    assert is_adult(2007, 2, on=date(2025, 2, 28))


def test_december_turns_18_on_the_31st() -> None:
    assert not is_adult(2008, 12, on=date(2026, 12, 30))
    assert is_adult(2008, 12, on=date(2026, 12, 31))
    assert not is_adult(2008, 12, on=date(2026, 1, 1))


def test_a_future_birth_month_is_not_adult() -> None:
    assert not is_adult(2026, 7, on=date(2026, 6, 30))
    assert not is_adult(2030, 1, on=date(2026, 6, 30))


def test_age_12_is_not_adult() -> None:
    assert not is_adult(2014, 1, on=date(2026, 6, 30))


def test_well_past_18_is_adult() -> None:
    assert is_adult(1960, 12, on=date(2026, 6, 30))
