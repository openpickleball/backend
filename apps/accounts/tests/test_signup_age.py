"""The age screen on allauth's headless signup, for both clients (#114).

Signup asks for a birth year and month and a gender division (#215).
Under 13 is refused with one fixed answer, before allauth reads the
email, and the session cannot try again. Made-up addresses on example.org
only.
"""

from importlib import import_module
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    ADULT_BIRTH,
    APP,
    BROWSER,
    PASSWORD,
    SIGNUP_GENDER_DIVISION,
    VERIFY_PREFIX,
    HeadlessSession,
    app_headers,
    csrf_header,
    key_from_link,
    link_in_last_mail,
)
from apps.emails.models import QueuedEmail
from apps.players.age import age_limit_date
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SIGNUP_REFUSED_SESSION_KEY,
    SignupDetails,
    take_signup_details,
)

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
EMAIL = "age.player@example.org"
MARKER = "8642097531"


Session = HeadlessSession


def _accounts() -> int:
    return get_user_model()._default_manager.count()


# Signup asks for the age


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("birth_year", ...),
        ("birth_month", ...),
        ("birth_year", None),
        ("birth_year", f"x{MARKER}"),
        ("birth_year", 1899),
        ("birth_year", 1990.5),
        ("birth_year", True),
        ("birth_year", [MARKER]),
        ("birth_year", "9" * 5000),
        ("birth_month", 0),
        ("birth_month", 13),
        ("birth_month", int(MARKER)),
        ("birth_month", f"{MARKER}x"),
    ],
)
def test_missing_or_malformed_age_is_a_field_error(
    base: str, field: str, value: object
) -> None:
    session = Session(base)

    response = session.signup(**{field: value})

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert [error.get("param") for error in errors] == [field]
    assert MARKER not in response.content.decode()
    assert _accounts() == 0
    assert not EmailAddress.objects.exists()
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_malformed_age_does_not_lock_the_session(base: str) -> None:
    session = Session(base)
    session.signup(birth_month=13)

    response = session.signup()

    assert response.status_code == 401
    assert _accounts() == 1


# Signup asks for the gender division (#215)


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "value",
    [..., None, "", "other", f"x{MARKER}", "MENS", " mens", 1, True, [MARKER]],
)
def test_missing_or_malformed_gender_division_is_a_field_error(
    base: str, value: object
) -> None:
    session = Session(base)

    response = session.signup(gender_division=value)

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert [error.get("param") for error in errors] == ["gender_division"]
    assert MARKER not in response.content.decode()
    assert _accounts() == 0
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []
    stored = session.store()
    assert SIGNUP_REFUSED_SESSION_KEY not in stored
    assert SIGNUP_DETAILS_SESSION_KEY not in stored


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (..., "Choose a gender division."),
        ("", "Choose a gender division."),
        ("other", "Choose one of the listed options."),
    ],
)
def test_gender_division_errors_have_fixed_messages(
    base: str, value: object, message: str
) -> None:
    response = Session(base).signup(gender_division=value)

    [error] = response.json()["errors"]
    assert error["message"] == message


@pytest.mark.parametrize("base", CLIENTS)
def test_a_missing_gender_division_with_an_under_13_age_is_not_refused(
    base: str,
) -> None:
    session = Session(base)
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 6}

    response = session.signup(gender_division=..., **young)

    assert response.status_code == 400
    assert [e.get("param") for e in response.json()["errors"]] == ["gender_division"]
    assert dict(session.store().items()) == {}


@pytest.mark.parametrize("base", CLIENTS)
def test_a_malformed_gender_division_does_not_lock_the_session(base: str) -> None:
    session = Session(base)
    session.signup(gender_division="other")

    response = session.signup()

    assert response.status_code == 401
    assert _accounts() == 1


def test_spec_lists_the_signup_details_as_required() -> None:
    spec = Client().get("/_allauth/openapi.json").json()
    base_signup = spec["components"]["schemas"]["BaseSignup"]
    details = {"birth_year", "birth_month", "gender_division"}

    assert details <= set(base_signup["required"])
    assert details <= set(base_signup["properties"])


# Under 13 is refused, neutrally, before anything else

REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]


def _under_13() -> list[dict[str, int]]:
    today = age_limit_date()
    return [
        {"birth_year": today.year - 12, "birth_month": 12},
        {"birth_year": today.year - 5, "birth_month": 6},
        {"birth_year": today.year, "birth_month": today.month},
        # A future month.
        {"birth_year": today.year + 1, "birth_month": 1},
        {"birth_year": 10**9, "birth_month": 1},
    ]


def _shape(response: Any) -> dict[str, Any]:
    """The response, with a session token's value replaced by its presence."""
    body = dict(response.json())
    meta = dict(body.get("meta", {}))
    if "session_token" in meta:
        meta["session_token"] = True
    if meta:
        body["meta"] = meta
    return {"status_code": response.status_code, "body": body}


def _assert_refused(response: Any) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 400
    assert body["errors"] == REFUSAL


def _assert_nothing_stored(accounts: int = 0, addresses: int = 0) -> None:
    assert _accounts() == accounts
    assert EmailAddress.objects.count() == addresses
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("index", range(5))
def test_under_13_is_refused(base: str, index: int) -> None:
    session = Session(base)

    response = session.signup(**_under_13()[index])

    _assert_refused(response)
    _assert_nothing_stored()
    assert dict(session.store().items()) == {SIGNUP_REFUSED_SESSION_KEY: True}


@pytest.mark.hold_on_commit
@pytest.mark.parametrize("base", CLIENTS)
def test_refusal_queues_no_email(base: str) -> None:
    UserFactory.create(email="taken.player@example.org")
    session = Session(base)

    _assert_refused(session.signup("taken.player@example.org", **_under_13()[1]))
    _assert_nothing_stored(accounts=1)


@pytest.mark.parametrize("base", CLIENTS)
def test_refusal_does_not_depend_on_the_email_or_password(base: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    young = _under_13()[1]
    variants: list[dict[str, object]] = [
        {"email": "taken.player@example.org"},
        {"email": "new.player@example.org"},
        {"email": "not-an-address"},
        {"email": "weak.player@example.org", "password": "1"},
        {"email": ...},
        {"email": "new.player@example.org", "password": ...},
    ]

    shapes = [_shape(Session(base).signup(**young, **v)) for v in variants]

    assert all(shape == shapes[0] for shape in shapes)
    assert shapes[0]["body"]["errors"] == REFUSAL
    _assert_nothing_stored(accounts=1, addresses=1)


# The same session cannot retry


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "retry",
    [
        {},
        {"email": "other.player@example.org"},
        {"birth_month": 13},
        {"birth_year": ..., "birth_month": ..., "email": ..., "password": ...},
    ],
)
def test_a_refused_session_is_refused_again(base: str, retry: dict[str, Any]) -> None:
    session = Session(base)
    first = session.signup(**_under_13()[1])

    again = session.signup(**retry)

    _assert_refused(again)
    assert _shape(again)["body"]["errors"] == _shape(first)["body"]["errors"]
    _assert_nothing_stored()
    assert dict(session.store().items()) == {SIGNUP_REFUSED_SESSION_KEY: True}


def test_app_refusal_gives_a_token_that_carries_the_refusal() -> None:
    session = Session(APP)

    first = session.signup(**_under_13()[1])
    token = first.json()["meta"]["session_token"]
    again = session.signup()

    assert token
    assert session.token == token
    _assert_refused(again)
    assert "meta" not in again.json() or "session_token" not in again.json()["meta"]
    _assert_nothing_stored()


def test_app_refusal_in_a_session_that_had_a_token() -> None:
    session = Session(APP)
    session.signup("first.player@example.org")
    token = session.token
    mail.outbox.clear()

    refused = session.signup("second.player@example.org", **_under_13()[1])
    again = session.signup("third.player@example.org")

    assert token
    assert session.token == token
    _assert_refused(refused)
    _assert_refused(again)
    assert _accounts() == 1
    assert mail.outbox == []
    stored = session.store()
    assert stored[SIGNUP_REFUSED_SESSION_KEY] is True
    assert SIGNUP_DETAILS_SESSION_KEY not in stored


def test_a_fresh_session_is_not_refused() -> None:
    Session(BROWSER).signup(**_under_13()[1])

    response = Session(BROWSER).signup()

    assert response.status_code == 401
    assert _accounts() == 1


# 13 and older signs up as before


def _pending_flows(response: Any) -> list[str]:
    flows = response.json()["data"]["flows"]
    return [flow["id"] for flow in flows if flow.get("is_pending")]


@pytest.mark.parametrize("base", CLIENTS)
def test_13_or_older_signs_up_and_holds_the_details(base: str) -> None:
    session = Session(base)
    year = age_limit_date().year - 14

    response = session.signup(birth_year=year, birth_month=1, gender_division="mens")

    assert response.status_code == 401
    assert _pending_flows(response) == ["verify_email"]
    assert [message.to for message in mail.outbox] == [[EMAIL]]
    account = get_user_model()._default_manager.get_by_natural_key(EMAIL)
    stored = session.store()
    assert stored[SIGNUP_DETAILS_SESSION_KEY] == {
        "account": account.pk,
        "year": year,
        "month": 1,
        "gender_division": "mens",
    }
    assert SIGNUP_REFUSED_SESSION_KEY not in stored


@pytest.mark.parametrize("base", CLIENTS)
def test_taken_address_holds_the_details_with_no_account(base: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    session = Session(base)

    response = session.signup("taken.player@example.org")

    assert response.status_code == 401
    assert session.store()[SIGNUP_DETAILS_SESSION_KEY] == {
        "account": None,
        "year": ADULT_BIRTH["birth_year"],
        "month": ADULT_BIRTH["birth_month"],
        "gender_division": SIGNUP_GENDER_DIVISION,
    }
    assert _accounts() == 1


def _outline(response: Any) -> dict[str, Any]:
    body = response.json()
    return {
        "status": response.status_code,
        "keys": sorted(body),
        "meta": sorted(body["meta"]),
        "flows": [flow["id"] for flow in body["data"]["flows"]],
        "token": "session_token" in body["meta"],
    }


@pytest.mark.parametrize("base", CLIENTS)
def test_new_and_taken_addresses_answer_alike(base: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)

    new = Session(base).signup("new.player@example.org")
    existing = Session(base).signup("taken.player@example.org")

    assert _outline(new) == _outline(existing)
    assert _outline(new)["token"] is (base == APP)


# The held details are handed over once, to the right Account


def _verify_then_log_in(session: Session) -> Any:
    """Verify by the emailed key, then log in, all in the signup's session.

    Verifying by link does not log in (ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION
    is off), so the client logs in next, in the same session.
    """
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    verified = session.post("auth/email/verify", {"key": key})
    assert verified.status_code == 401
    assert verified.json()["meta"]["is_authenticated"] is False
    logged_in = session.post("auth/login", {"email": EMAIL, "password": PASSWORD})
    assert logged_in.status_code == 200
    assert logged_in.json()["meta"]["is_authenticated"] is True
    return logged_in


@pytest.mark.parametrize("base", CLIENTS)
def test_the_details_are_taken_once_after_verify_and_login(base: str) -> None:
    session = Session(base)
    session.signup()
    before = session.store().session_key

    _verify_then_log_in(session)

    account = get_user_model()._default_manager.get_by_natural_key(EMAIL)
    other = UserFactory.create()
    stored = session.store()
    # Login cycles the session key and keeps the session's data.
    assert stored.session_key != before
    assert take_signup_details(stored, other) is None
    assert take_signup_details(stored, account) == SignupDetails(
        birth_year=ADULT_BIRTH["birth_year"],
        birth_month=ADULT_BIRTH["birth_month"],
        gender_division=SIGNUP_GENDER_DIVISION,
    )
    assert take_signup_details(stored, account) is None
    stored.save()
    assert take_signup_details(session.store(), account) is None


@pytest.mark.parametrize("base", CLIENTS)
def test_a_login_to_another_account_never_gets_the_age(base: str) -> None:
    other = UserFactory.create(email="other.player@example.org", password=PASSWORD)
    EmailAddress.objects.create(user=other, email=other.email, verified=True)
    session = Session(base)
    session.signup()

    logged_in = session.post(
        "auth/login", {"email": "other.player@example.org", "password": PASSWORD}
    )

    assert logged_in.status_code == 200
    assert take_signup_details(session.store(), other) is None


@pytest.mark.parametrize("base", CLIENTS)
def test_logout_removes_the_held_details(base: str) -> None:
    session = Session(base)
    session.signup()
    _verify_then_log_in(session)
    assert SIGNUP_DETAILS_SESSION_KEY in session.store()
    before = session.store().session_key

    if base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = app_headers(session.token)
    session.client.delete(f"{base}/auth/session", headers=headers)

    assert SIGNUP_DETAILS_SESSION_KEY not in session.store()
    store_class = import_module(settings.SESSION_ENGINE).SessionStore
    assert not store_class().exists(before)
