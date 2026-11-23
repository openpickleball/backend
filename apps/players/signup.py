"""The signup details held in the session (issues #114 and #215).

Signup asks for the birth year and month and the gender division before
an Account exists (ADR 0017). The signup view holds them in the session,
bound to the new Account (``hold_signup_details``), and the Player create
endpoint (#124) reads them (``peek_signup_details``) and takes them once
the Player exists (``take_signup_details``). The session never holds a
day of birth or a raw gender (ADR 0009).

The accounts app imports this module, so it never imports that app back.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.players.models import GenderDivision

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser
    from django.contrib.sessions.backends.base import SessionBase

# The details a signup gave, until the Player create endpoint takes them.
SIGNUP_DETAILS_SESSION_KEY = "open_signup_details"
# Set when a signup was refused for age: the session cannot retry.
SIGNUP_REFUSED_SESSION_KEY = "open_signup_refused"


@dataclass(frozen=True)
class SignupDetails:
    """A signup's birth year and month and gender division."""

    birth_year: int
    birth_month: int
    gender_division: str


def hold_signup_details(
    session: "SessionBase",
    account: "AbstractBaseUser | None",
    details: SignupDetails,
) -> None:
    """Keep a signup's details in the session.

    Bound to ``account`` (None when signup created no Account), so a later
    login to another Account in the same session never receives them. The
    value holds the Account's pk, which stays on the server.
    """
    session[SIGNUP_DETAILS_SESSION_KEY] = {
        "account": account.pk if account is not None else None,
        "year": details.birth_year,
        "month": details.birth_month,
        "gender_division": details.gender_division,
    }


def peek_signup_details(
    session: "SessionBase", account: "AbstractBaseUser"
) -> SignupDetails | None:
    """The details held for ``account``, left in the session.

    None when nothing is held, it is held for another Account (or none),
    or the held value is malformed. The Player create endpoint reads them
    first and takes them only once the Player exists, so a create that
    fails keeps them for a retry.
    """
    held = session.get(SIGNUP_DETAILS_SESSION_KEY)
    if not isinstance(held, dict) or account.pk is None:
        return None
    if held.get("account") != account.pk:
        return None
    year, month = held.get("year"), held.get("month")
    division = held.get("gender_division")
    if not isinstance(year, int) or isinstance(year, bool):
        return None
    if not isinstance(month, int) or isinstance(month, bool):
        return None
    if not isinstance(division, str) or division not in GenderDivision.values:
        return None
    return SignupDetails(birth_year=year, birth_month=month, gender_division=division)


def take_signup_details(
    session: "SessionBase", account: "AbstractBaseUser"
) -> SignupDetails | None:
    """The details held for ``account``, removed once taken.

    None, with the session left as it was, when nothing is held or it is
    held for another Account (or none).
    """
    details = peek_signup_details(session, account)
    if details is not None:
        del session[SIGNUP_DETAILS_SESSION_KEY]
    return details
