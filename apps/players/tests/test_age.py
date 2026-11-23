"""The age rules: end-of-month birthdays and the earliest date on Earth."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.utils import timezone

from apps.players import age
from apps.players.age import (
    ADULT_AGE,
    MINIMUM_AGE,
    age_limit_date,
    age_on,
    is_minor,
    is_old_enough,
)


def test_limits() -> None:
    assert (MINIMUM_AGE, ADULT_AGE) == (13, 18)


# The end-of-month rule


@pytest.mark.parametrize(
    ("birth_year", "birth_month", "on", "expected"),
    [
        # A 31-day month: born 31 January 2012.
        (2012, 1, date(2025, 1, 30), 12),
        (2012, 1, date(2025, 1, 31), 13),
        # A 30-day month: born 30 April 2012.
        (2012, 4, date(2025, 4, 29), 12),
        (2012, 4, date(2025, 4, 30), 13),
        # February in a leap year: born 29 February 2012.
        (2012, 2, date(2025, 2, 28), 12),
        (2012, 2, date(2025, 3, 1), 13),
        # February in a common year: born 28 February 2013.
        (2013, 2, date(2026, 2, 27), 12),
        (2013, 2, date(2026, 2, 28), 13),
        # December across the year boundary: born 31 December 2011.
        (2011, 12, date(2024, 12, 30), 12),
        (2011, 12, date(2024, 12, 31), 13),
        (2011, 12, date(2025, 1, 1), 13),
        # The adult limit.
        (2007, 6, date(2025, 6, 29), 17),
        (2007, 6, date(2025, 6, 30), 18),
        (2007, 12, date(2025, 12, 30), 17),
        (2007, 12, date(2025, 12, 31), 18),
    ],
)
def test_age_counts_from_the_last_day_of_the_birth_month(
    birth_year: int, birth_month: int, on: date, expected: int
) -> None:
    assert age_on(birth_year, birth_month, on) == expected


@pytest.mark.parametrize("birth_year", [1900, 1999, 2400, 9999, 10_000, 10**9])
@pytest.mark.parametrize("birth_month", range(1, 13))
def test_age_never_raises(birth_year: int, birth_month: int) -> None:
    result = age_on(birth_year, birth_month, date(2026, 11, 17))

    assert isinstance(result, int)


def test_a_future_month_is_not_old_enough() -> None:
    on = date(2026, 11, 17)

    assert not is_old_enough(2027, 1, on)
    assert not is_minor(2027, 1, on)


@pytest.mark.parametrize(
    ("on", "old_enough", "minor"),
    [
        (date(2025, 4, 29), False, False),
        (date(2025, 4, 30), True, True),
        (date(2030, 4, 29), True, True),
        (date(2030, 4, 30), True, False),
    ],
)
def test_old_enough_and_minor_flip_at_13_and_18(
    on: date, old_enough: bool, minor: bool
) -> None:
    # Born 30 April 2012.
    assert is_old_enough(2012, 4, on) is old_enough
    assert is_minor(2012, 4, on) is minor


# Which date


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


def test_age_limit_date_is_the_date_at_utc_minus_12(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _at(monkeypatch, datetime(2026, 5, 31, 5, 0, tzinfo=UTC))
    assert age_limit_date() == date(2026, 5, 30)

    _at(monkeypatch, datetime(2026, 5, 31, 12, 0, tzinfo=UTC))
    assert age_limit_date() == date(2026, 5, 31)


def test_the_age_limit_waits_for_the_last_place_on_earth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Born 31 May 2013. At 05:00 UTC on 31 May 2026 it is still 30 May
    # at UTC-12.
    _at(monkeypatch, datetime(2026, 5, 31, 5, 0, tzinfo=UTC))
    assert not is_old_enough(2013, 5)

    _at(monkeypatch, datetime(2026, 5, 31, 12, 0, tzinfo=UTC))
    assert is_old_enough(2013, 5)


def test_minor_defaults_to_the_age_limit_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Born 31 May 2008: 18 on 31 May 2026 at UTC-12.
    _at(monkeypatch, datetime(2026, 5, 31, 5, 0, tzinfo=UTC))
    assert is_minor(2008, 5)

    _at(monkeypatch, datetime(2026, 5, 31, 12, 0, tzinfo=UTC))
    assert not is_minor(2008, 5)


def test_age_module_does_not_import_accounts() -> None:
    assert "apps.accounts" not in Path(age.__file__).read_text()
