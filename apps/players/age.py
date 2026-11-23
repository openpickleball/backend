"""A player's age from their birth year and month (issue #114, ADR 0009).

OPEN never stores the day of birth, so a player counts as born on the last
day of their birth month: the latest day they could have been born, so no
one under a limit is counted as over it. Every age limit decision uses the
earliest calendar date anywhere on Earth, the date at UTC-12
(``age_limit_date``), so a birthday has begun everywhere before it counts.

Signup asks for the birth year and month before an Account exists. The
signup view holds them in the session, bound to the new Account
(``hold_signup_birth``), and the Player create endpoint (#124) reads them
(``peek_signup_birth``) and takes them once the Player exists
(``take_signup_birth``). The session never holds a day.
"""

import calendar
from datetime import UTC, date, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.sessions.backends.base import SessionBase

MINIMUM_AGE = 13
ADULT_AGE = 18

# The age a signup gave, until the Player create endpoint takes it.
SIGNUP_BIRTH_SESSION_KEY = "open_signup_birth"
# Set when a signup was refused for age: the session cannot retry.
SIGNUP_REFUSED_SESSION_KEY = "open_signup_refused"

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


def hold_signup_birth(
    session: "SessionBase",
    account: "AbstractBaseUser | None",
    year: int,
    month: int,
) -> None:
    """Keep a signup's birth year and month in the session.

    Bound to ``account`` (None when signup created no Account), so a later
    login to another Account in the same session never receives it. The
    value holds the Account's pk, which stays on the server.
    """
    session[SIGNUP_BIRTH_SESSION_KEY] = {
        "account": account.pk if account is not None else None,
        "year": year,
        "month": month,
    }


def peek_signup_birth(
    session: "SessionBase", account: "AbstractBaseUser"
) -> tuple[int, int] | None:
    """The held ``(year, month)`` for ``account``, left in the session.

    None when nothing is held or it is held for another Account (or none).
    The Player create endpoint reads it first and takes it only once the
    Player exists, so a create that fails keeps it for a retry.
    """
    held = session.get(SIGNUP_BIRTH_SESSION_KEY)
    if not isinstance(held, dict) or account.pk is None:
        return None
    if held.get("account") != account.pk:
        return None
    year, month = held.get("year"), held.get("month")
    if not isinstance(year, int) or not isinstance(month, int):
        return None
    return year, month


def take_signup_birth(
    session: "SessionBase", account: "AbstractBaseUser"
) -> tuple[int, int] | None:
    """The held ``(year, month)`` for ``account``, removed once taken.

    None, with the session left as it was, when nothing is held or it is
    held for another Account (or none).
    """
    birth = peek_signup_birth(session, account)
    if birth is not None:
        del session[SIGNUP_BIRTH_SESSION_KEY]
    return birth
