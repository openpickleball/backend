"""The signup details held in the session (issues #114, #215, #221).

Signup holds the birth year and month, the gender division, the legal
name, and the city and region, bound to the new Account, until the Player
create endpoint takes them. Made-up names only.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore

from apps.players import signup
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SIGNUP_REFUSED_SESSION_KEY,
    SignupDetails,
    hold_signup_details,
    is_signup_refused,
    peek_signup_details,
    refuse_signup,
    take_signup_details,
)

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

DETAILS = SignupDetails(
    birth_year=2000,
    birth_month=4,
    gender_division="mens",
    legal_name="Sam Quillfeather",
    city="Lakeview",
    region="",
)
# A well-formed hold for Account 7, which the malformed cases change.
HELD = {
    "account": 7,
    "year": 2000,
    "month": 4,
    "gender_division": "mens",
    "legal_name": "Sam Quillfeather",
    "city": "Lakeview",
    "region": "",
}


def _account(pk: int) -> "AbstractBaseUser":
    # An unsaved Account with a pk: the helpers read only the pk.
    account: AbstractBaseUser = get_user_model()(pk=pk)
    return account


def test_session_keys() -> None:
    assert SIGNUP_DETAILS_SESSION_KEY == "open_signup_details"
    assert SIGNUP_REFUSED_SESSION_KEY == "open_signup_refused"


def test_held_details_are_handed_once_to_their_account() -> None:
    session = SessionStore()
    hold_signup_details(session, _account(7), DETAILS)

    assert session[SIGNUP_DETAILS_SESSION_KEY] == HELD
    assert take_signup_details(session, _account(7)) == DETAILS
    assert SIGNUP_DETAILS_SESSION_KEY not in session
    assert take_signup_details(session, _account(7)) is None


def test_held_details_are_not_handed_to_another_account() -> None:
    session = SessionStore()
    hold_signup_details(session, _account(7), DETAILS)

    assert take_signup_details(session, _account(8)) is None
    assert session[SIGNUP_DETAILS_SESSION_KEY]["account"] == 7


def test_details_held_without_an_account_are_handed_to_nobody() -> None:
    session = SessionStore()
    hold_signup_details(session, None, DETAILS)

    assert session[SIGNUP_DETAILS_SESSION_KEY]["account"] is None
    assert take_signup_details(session, _account(7)) is None
    assert SIGNUP_DETAILS_SESSION_KEY in session


def test_take_ignores_a_malformed_value() -> None:
    session = SessionStore()
    session[SIGNUP_DETAILS_SESSION_KEY] = "not a dict"

    assert take_signup_details(session, _account(7)) is None


def test_peek_returns_the_held_details_and_keeps_them() -> None:
    session = SessionStore()
    hold_signup_details(session, _account(7), DETAILS)

    session.modified = False

    assert peek_signup_details(session, _account(7)) == DETAILS
    assert peek_signup_details(session, _account(7)) == DETAILS
    assert session.modified is False
    assert take_signup_details(session, _account(7)) == DETAILS


def test_peek_hands_nothing_to_another_account_or_nobody() -> None:
    session = SessionStore()
    hold_signup_details(session, _account(7), DETAILS)
    unbound = SessionStore()
    hold_signup_details(unbound, None, DETAILS)

    assert peek_signup_details(session, _account(8)) is None
    assert peek_signup_details(unbound, _account(7)) is None
    assert peek_signup_details(SessionStore(), _account(7)) is None


@pytest.mark.parametrize(
    "held",
    [
        {**HELD, "year": "2000"},
        {**HELD, "month": True},
        {k: v for k, v in HELD.items() if k != "gender_division"},
        {**HELD, "gender_division": ""},
        {**HELD, "gender_division": "other"},
        {**HELD, "gender_division": None},
        # A hold from before #221, with no name or place.
        {"account": 7, "year": 2000, "month": 4, "gender_division": "mens"},
        {**HELD, "legal_name": ""},
        {**HELD, "legal_name": None},
        {**HELD, "legal_name": 5},
        {k: v for k, v in HELD.items() if k != "city"},
        {**HELD, "city": None},
        {**HELD, "region": 5},
    ],
)
def test_peek_ignores_a_malformed_value(held: dict[str, object]) -> None:
    session = SessionStore()
    session[SIGNUP_DETAILS_SESSION_KEY] = held

    assert peek_signup_details(session, _account(7)) is None
    assert SIGNUP_DETAILS_SESSION_KEY in session


def test_an_old_birth_only_hold_is_ignored() -> None:
    # The key held before #215, with no gender division.
    session = SessionStore()
    session["open_signup_birth"] = {"account": 7, "year": 2000, "month": 4}

    assert peek_signup_details(session, _account(7)) is None


def test_signup_module_does_not_import_accounts() -> None:
    assert "apps.accounts" not in Path(signup.__file__).read_text()


def test_refuse_signup_sets_the_flag_and_drops_the_held_details() -> None:
    session = SessionStore()
    hold_signup_details(session, _account(7), DETAILS)

    refuse_signup(session)

    assert dict(session.items()) == {SIGNUP_REFUSED_SESSION_KEY: True}
    assert is_signup_refused(session)


def test_refuse_signup_with_nothing_held_sets_only_the_flag() -> None:
    session = SessionStore()

    refuse_signup(session)

    assert dict(session.items()) == {SIGNUP_REFUSED_SESSION_KEY: True}


@pytest.mark.parametrize("value", [None, False, 1, "true", "yes"])
def test_only_true_counts_as_refused(value: object) -> None:
    session = SessionStore()
    if value is not None:
        session[SIGNUP_REFUSED_SESSION_KEY] = value

    assert not is_signup_refused(session)
