"""The signup details held in the session (issues #114, #215, and #221).

Signup asks for the birth year and month, the gender division, the first
and last name, and optionally the city and region, before an Account
exists (ADR 0017, ADR 0019, ADR 0021). The signup view holds them in the session,
bound to the new Account (``hold_signup_details``), and the Player create
endpoint (#124) reads them (``peek_signup_details``) and takes them once
the Player exists (``take_signup_details``). The session never holds a
day of birth or a raw gender (ADR 0009). The names are held as one legal
name, "First Last", with the first name, so the Player create endpoint
can start a player aged 13 to 17 with the display name "First L."
(ADR 0022).

An under-13 age, at signup, at the signup age check (#223), or at the
Player create endpoint, refuses the session (``refuse_signup``): it drops
any held details and sets a flag. All three honor the flag
(``is_signup_refused``), and logout keeps it.

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
# Set when a signup, a signup age check, or a Player create was refused
# for age: the session cannot retry any of them.
SIGNUP_REFUSED_SESSION_KEY = "open_signup_refused"


def is_signup_refused(session: "SessionBase") -> bool:
    """Whether an under-13 age refused this session."""
    return session.get(SIGNUP_REFUSED_SESSION_KEY) is True


def refuse_signup(session: "SessionBase") -> None:
    """Refuse the session for age: drop any held details, set the flag.

    A flag only, never the age or the gender division (ADR 0009).
    """
    session.pop(SIGNUP_DETAILS_SESSION_KEY, None)
    session[SIGNUP_REFUSED_SESSION_KEY] = True


@dataclass(frozen=True)
class SignupDetails:
    """A signup's birth year and month, gender division, legal name
    ("First Last"), city and region (empty when not given), and first
    name (the start of the legal name)."""

    birth_year: int
    birth_month: int
    gender_division: str
    legal_name: str
    city: str
    region: str
    first_name: str

    @property
    def last_name(self) -> str:
        """The legal name after the first name and its space."""
        return self.legal_name[len(self.first_name) + 1 :]


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
        "legal_name": details.legal_name,
        "city": details.city,
        "region": details.region,
        "first_name": details.first_name,
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
    legal_name, city, region = (held.get(k) for k in ("legal_name", "city", "region"))
    if not isinstance(legal_name, str) or not legal_name:
        return None
    if not isinstance(city, str) or not isinstance(region, str):
        return None
    first_name = held.get("first_name")
    if not isinstance(first_name, str) or not first_name:
        return None
    # The legal name is "First Last": the first name, a space, and more.
    if (
        not legal_name.startswith(f"{first_name} ")
        or len(legal_name) <= len(first_name) + 1
    ):
        return None
    return SignupDetails(
        birth_year=year,
        birth_month=month,
        gender_division=division,
        legal_name=legal_name,
        city=city,
        region=region,
        first_name=first_name,
    )


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
