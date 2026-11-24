"""The signup age check, for both headless clients (issue #223, ADR 0020).

The app asks for the birth year and month after the email and password,
and checks them before it asks for a name or a location. The check uses
the signup age screen's rules: 13 or older passes and the session keeps
nothing; under 13 gets the same fixed refusal and flags the session, so
the check and every later signup in it are refused. It reads no email
address. Made-up addresses on example.org only.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
import yaml
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.adapter import AccountAdapter
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    ADULT_BIRTH,
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
    app_headers,
    csrf_header,
)
from apps.emails.models import QueuedEmail
from apps.players.age import age_limit_date
from apps.players.signup import (
    SIGNUP_DETAILS_SESSION_KEY,
    SIGNUP_REFUSED_SESSION_KEY,
)

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
MARKER = "8642097531"
LIMIT = 20
SIGNED_IN = "signed.in@example.org"
SPEC_PATH = "/_allauth/{client}/v1/auth/signup/age-check"

REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]


def _accounts() -> int:
    return get_user_model()._default_manager.count()


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


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


def _assert_refused(response: Any) -> None:
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == 400
    assert body["errors"] == REFUSAL


def _assert_nothing_stored(accounts: int = 0) -> None:
    assert _accounts() == accounts
    assert EmailAddress.objects.count() == 0
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []


def _shape(response: Any) -> dict[str, Any]:
    """The response, with a session token's value replaced by its presence."""
    body = dict(response.json())
    meta = dict(body.get("meta", {}))
    if "session_token" in meta:
        meta["session_token"] = True
    if meta:
        body["meta"] = meta
    return {"status_code": response.status_code, "body": body}


# 13 or older passes and stores nothing


@pytest.mark.parametrize("base", CLIENTS)
def test_13_or_older_passes_and_the_session_keeps_nothing(base: str) -> None:
    session = HeadlessSession(base)

    response = session.age_check(**ADULT_BIRTH)

    assert response.status_code == 200
    assert response.json() == {"status": 200}
    assert dict(session.store().items()) == {}
    # The app client gets no session token: there is no session to keep.
    assert session.token is None
    _assert_nothing_stored()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_pass_leaves_held_signup_details_as_they_were(base: str) -> None:
    session = HeadlessSession(base)
    assert session.signup().status_code == 401
    before = dict(session.store().items())
    assert SIGNUP_DETAILS_SESSION_KEY in before

    response = session.age_check(**ADULT_BIRTH)

    assert response.status_code == 200
    assert dict(session.store().items()) == before


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    ("moment", "status"),
    [
        # 31 May at UTC-12 has not begun: still 12 there.
        (datetime(2026, 5, 31, 11, 59, tzinfo=UTC), 400),
        (datetime(2026, 5, 31, 12, 0, tzinfo=UTC), 200),
    ],
)
def test_the_end_of_month_rule_at_utc_minus_12(
    base: str, moment: datetime, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)
    session = HeadlessSession(base)

    response = session.age_check(birth_year=2013, birth_month=5)

    assert response.status_code == status


# Under 13 is refused and flags the session


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("index", range(5))
def test_under_13_is_refused_and_flags_the_session(base: str, index: int) -> None:
    session = HeadlessSession(base)

    response = session.age_check(**_under_13()[index])

    _assert_refused(response)
    assert "param" not in response.json()["errors"][0]
    assert dict(session.store().items()) == {SIGNUP_REFUSED_SESSION_KEY: True}
    _assert_nothing_stored()


def test_an_app_client_with_no_token_gets_one_with_the_refusal() -> None:
    session = HeadlessSession(APP)

    _assert_refused(session.age_check(**_young()))

    assert session.token is not None


@pytest.mark.parametrize("base", CLIENTS)
def test_a_refusal_drops_held_signup_details(base: str) -> None:
    session = HeadlessSession(base)
    assert session.signup().status_code == 401

    _assert_refused(session.age_check(**_young()))

    store = session.store()
    assert SIGNUP_DETAILS_SESSION_KEY not in store
    assert store[SIGNUP_REFUSED_SESSION_KEY] is True


# A flagged session is refused, whatever age it sends


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "later",
    [dict(ADULT_BIRTH), {"birth_month": 13}, {"birth_year": ...}, {"x": MARKER}],
)
def test_a_refused_check_refuses_every_later_check(
    base: str, later: dict[str, object]
) -> None:
    session = HeadlessSession(base)
    first = session.age_check(**_young())

    response = session.age_check(**later)

    _assert_refused(response)
    assert _shape(response)["body"]["errors"] == _shape(first)["body"]["errors"]


@pytest.mark.parametrize("base", CLIENTS)
def test_a_refused_check_refuses_signup(base: str) -> None:
    session = HeadlessSession(base)
    _assert_refused(session.age_check(**_young()))

    response = session.signup()

    _assert_refused(response)
    _assert_nothing_stored()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_refused_signup_refuses_the_check(base: str) -> None:
    session = HeadlessSession(base)
    _assert_refused(session.signup(**_young()))

    response = session.age_check(**ADULT_BIRTH)

    _assert_refused(response)


@pytest.mark.parametrize("base", CLIENTS)
def test_a_refused_check_stays_refused_after_login_and_logout(base: str) -> None:
    account = UserFactory.create(email=SIGNED_IN, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=SIGNED_IN, verified=True)
    session = HeadlessSession(base)
    _assert_refused(session.age_check(**_young()))
    login = session.post("auth/login", {"email": SIGNED_IN, "password": PASSWORD})
    assert login.status_code == 200
    headers = app_headers(session.token) if base == APP else csrf_header(session.client)
    logout = session.client.delete(f"{base}/auth/session", headers=headers)
    assert logout.status_code == 401
    if base == APP:
        token = logout.json().get("meta", {}).get("session_token")
        if token:
            session.token = token

    response = session.age_check(**ADULT_BIRTH)

    _assert_refused(response)


# A missing or malformed age is a field error, as at signup


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
def test_missing_or_malformed_age_is_signups_field_error(
    base: str, field: str, value: object
) -> None:
    session = HeadlessSession(base)

    response = session.age_check(**{field: value})

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert [error.get("param") for error in errors] == [field]
    assert MARKER not in response.content.decode()
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()
    _assert_nothing_stored()
    signup = HeadlessSession(base).signup("other.player@example.org", **{field: value})
    assert signup.status_code == 400
    assert signup.json()["errors"] == errors


@pytest.mark.parametrize("base", CLIENTS)
def test_an_empty_body_names_both_fields(base: str) -> None:
    session = HeadlessSession(base)

    response = session.post("auth/signup/age-check", {})

    assert response.status_code == 400
    params = [error.get("param") for error in response.json()["errors"]]
    assert params == ["birth_year", "birth_month"]
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_malformed_age_does_not_flag_the_session(base: str) -> None:
    session = HeadlessSession(base)
    assert session.age_check(birth_month=13).status_code == 400

    response = session.age_check(**ADULT_BIRTH)

    assert response.status_code == 200


# The check reveals nothing about any email address


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", ["adult", "young"])
def test_the_answer_does_not_depend_on_an_email_address(base: str, age: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    birth = dict(ADULT_BIRTH) if age == "adult" else _young()
    extras: list[dict[str, object]] = [
        {},
        {"email": "taken.player@example.org"},
        {"email": "TAKEN.player@example.org"},
        {"email": "new.player@example.org"},
        {"email": "not an address", "password": PASSWORD},
    ]
    user_table = get_user_model()._meta.db_table
    address_table = EmailAddress._meta.db_table
    shapes = []
    for extra in extras:
        session = HeadlessSession(base)
        with CaptureQueriesContext(connection) as queries:
            response = session.post("auth/signup/age-check", {**birth, **extra})
        shapes.append(_shape(response))
        sql = " ".join(query["sql"] for query in queries.captured_queries)
        assert user_table not in sql
        assert address_table not in sql

    assert all(shape == shapes[0] for shape in shapes)
    assert EmailAddress.objects.count() == 1
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []


# allauth's answers and its signup rate limit come first


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", ["young", "malformed"])
def test_a_signed_in_session_gets_409_and_no_flag(base: str, age: str) -> None:
    account = UserFactory.create(email=SIGNED_IN, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=SIGNED_IN, verified=True)
    session = HeadlessSession(base)
    login = session.post("auth/login", {"email": SIGNED_IN, "password": PASSWORD})
    assert login.status_code == 200

    birth = _young() if age == "young" else {"birth_month": 13}
    response = session.age_check(**birth)

    assert response.status_code == 409
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("age", ["young", "malformed"])
def test_a_closed_signup_gets_403_and_no_flag(
    base: str, age: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AccountAdapter, "is_open_for_signup", lambda *_: False)
    session = HeadlessSession(base)

    birth = _young() if age == "young" else {"birth_month": 13}
    response = session.age_check(**birth)

    assert response.status_code == 403
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()


@pytest.mark.parametrize("base", CLIENTS)
def test_checks_count_once_against_the_signup_limit(base: str) -> None:
    statuses = [HeadlessSession(base).age_check().status_code for _ in range(LIMIT)]
    late = HeadlessSession(base)

    check = late.age_check(**_young())
    signup = HeadlessSession(base).signup("late.player@example.org")

    assert statuses == [200] * LIMIT
    assert check.status_code == 429
    # The 429 comes before the flag.
    assert SIGNUP_REFUSED_SESSION_KEY not in late.store()
    assert signup.status_code == 429
    assert _accounts() == 0


@pytest.mark.parametrize("base", CLIENTS)
def test_signups_and_checks_share_one_limit(base: str) -> None:
    half = LIMIT // 2
    statuses = [
        HeadlessSession(base).signup(f"player{n}@example.org").status_code
        for n in range(half)
    ]
    statuses += [HeadlessSession(base).age_check().status_code for _ in range(half)]

    response = HeadlessSession(base).age_check()

    assert statuses == [401] * half + [200] * half
    assert response.status_code == 429


# allauth's OpenAPI document lists the check


def _spec_operation(spec: dict[str, Any]) -> dict[str, Any]:
    operation: dict[str, Any] = spec["paths"][SPEC_PATH]["post"]
    return operation


@pytest.mark.parametrize("document", ["openapi.json", "openapi.yaml"])
def test_the_openapi_document_lists_the_check(document: str) -> None:
    response = Client().get(f"/_allauth/{document}")
    assert response.status_code == 200
    spec = yaml.safe_load(response.content)

    operation = _spec_operation(spec)

    assert {"$ref": "#/components/parameters/Client"} in operation["parameters"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert set(body["required"]) == {"birth_year", "birth_month"}
    assert set(body["properties"]) == {"birth_year", "birth_month"}
    assert {"200", "400", "403", "409", "429"} <= set(operation["responses"])
    # allauth's own paths are all still there.
    assert "/_allauth/{client}/v1/auth/signup" in spec["paths"]
