"""A player's age from their birth year and month (issue #114, ADR 0009).

OPEN never stores the day of birth, so a player counts as born on the last
day of their birth month: the latest day they could have been born, so no
one under a limit is counted as over it. Every age limit decision uses the
earliest calendar date anywhere on Earth, the date at UTC-12
(``age_limit_date``), so a birthday has begun everywhere before it counts.

Signup asks for the birth year and month before an Account exists.
``apps.players.signup`` holds them in the session, with the gender
division, until the Player create endpoint (#124) takes them.
"""

import calendar
from datetime import UTC, date, timedelta

from django.utils import timezone

MINIMUM_AGE = 13
ADULT_AGE = 18

# The earliest time zone in use (Baker Island, UTC-12).
_EARLIEST_OFFSET = timedelta(hours=-12)


def age_limit_date() -> date:
    """Today's date at UTC-12, the earliest current date on Earth."""
    return (timezone.now().astimezone(UTC) + _EARLIEST_OFFSET).date()


def _last_day(year: int, month: int) -> int:
    # calendar.monthrange goes through datetime and raises past year 9999.
    if month == 2:
        return 29 if calendar.isleap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def age_on(birth_year: int, birth_month: int, on: date) -> int:
    """Whole years old on ``on``, counting from the birth month's last day.

    Negative for a birth month after ``on``. Never raises for an int year
    and a month from 1 to 12.
    """
    birthday = (birth_month, _last_day(birth_year, birth_month))
    years = on.year - birth_year
    if (on.month, on.day) < birthday:
        years -= 1
    return years


def is_old_enough(birth_year: int, birth_month: int, on: date | None = None) -> bool:
    """Whether the player is at least 13 on ``on`` (the age limit date)."""
    return age_on(birth_year, birth_month, on or age_limit_date()) >= MINIMUM_AGE


def is_minor(birth_year: int, birth_month: int, on: date | None = None) -> bool:
    """Whether the player is 13 to 17 on ``on`` (the age limit date)."""
    years = age_on(birth_year, birth_month, on or age_limit_date())
    return MINIMUM_AGE <= years < ADULT_AGE


def is_adult(birth_year: int, birth_month: int, on: date | None = None) -> bool:
    """Whether the player is at least 18 on ``on`` (the age limit date)."""
    return age_on(birth_year, birth_month, on or age_limit_date()) >= ADULT_AGE
