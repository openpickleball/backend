"""The signup password check, for both headless clients (#289, ADR 0039).

The app asks for the email and password on its first signup screen and
checks the password before it asks for the age. The check runs signup's
password rules (``AUTH_PASSWORD_VALIDATORS``) and answers with signup's
errors. It looks up no Account or address and writes nothing to the
session. Made-up addresses on example.org only.
"""

from typing import Any

import pytest
import yaml
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from apps.accounts.adapter import AccountAdapter
from apps.accounts.openapi import open_schema
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    SESSION_EMAIL,
    HeadlessSession,
)
from apps.emails.models import QueuedEmail
from apps.players.age import age_limit_date
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY

pytestmark = pytest.mark.django_db

CLIENTS = [APP, BROWSER]
MARKER = "8642097531"
LIMIT = 20
SIGNED_IN = "signed.in@example.org"
SPEC_PATH = "/_allauth/{client}/v1/auth/signup/password-check"
# A made-up address, and a password made from its first part.
SIMILAR_EMAIL = "quillfeather.robin@example.org"
SIMILAR_PASSWORD = "quillfeather"

REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]

TOO_SHORT = {
    "message": "This password is too short. It must contain at least 8 characters.",
    "code": "password_too_short",
    "param": "password",
}
TOO_COMMON = {
    "message": "This password is too common.",
    "code": "password_too_common",
    "param": "password",
}
NUMERIC = {
    "message": "This password is entirely numeric.",
    "code": "password_entirely_numeric",
    "param": "password",
}
TOO_SIMILAR = {
    "message": "The password is too similar to the email address.",
    "code": "password_too_similar",
    "param": "password",
}

# Each password, the address sent with it, and the errors it gets, in
# the order of AUTH_PASSWORD_VALIDATORS.
FAILURES = [
    pytest.param(SIMILAR_PASSWORD, SIMILAR_EMAIL, [TOO_SIMILAR], id="similar"),
    pytest.param("Xq7#vR2", SESSION_EMAIL, [TOO_SHORT], id="too-short"),
    pytest.param("password", SESSION_EMAIL, [TOO_COMMON], id="common"),
    pytest.param("73948261055", SESSION_EMAIL, [NUMERIC], id="numeric"),
    pytest.param("12345678", SESSION_EMAIL, [TOO_COMMON, NUMERIC], id="common-numeric"),
    pytest.param(
        "1234", SESSION_EMAIL, [TOO_SHORT, TOO_COMMON, NUMERIC], id="short-numeric"
    ),
]


def _accounts() -> int:
    return get_user_model()._default_manager.count()


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


def _assert_nothing_stored(accounts: int = 0) -> None:
    assert _accounts() == accounts
    assert EmailAddress.objects.count() == 0
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []


# A strong password passes and stores nothing


@pytest.mark.parametrize("base", CLIENTS)
def test_a_strong_password_passes_and_the_session_keeps_nothing(base: str) -> None:
    session = HeadlessSession(base)

    response = session.password_check()

    assert response.status_code == 200
    assert response.json() == {"status": 200}
    assert dict(session.store().items()) == {}
    # The app client gets no session token: there is no session to keep.
    assert session.token is None
    _assert_nothing_stored()


# A failed rule is signup's error, one per rule


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(("password", "email", "errors"), FAILURES)
def test_a_failed_rule_gets_signups_errors(
    base: str, password: str, email: str, errors: list[dict[str, str]]
) -> None:
    session = HeadlessSession(base)

    response = session.password_check(email=email, password=password)

    assert response.status_code == 400
    assert response.json() == {"status": 400, "errors": errors}
    assert dict(session.store().items()) == {}
    assert session.token is None
    _assert_nothing_stored()
    signup = HeadlessSession(base).signup(email, password=password)
    assert signup.status_code == 400
    assert signup.json()["errors"] == errors
    assert _accounts() == 0


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_refuses_a_password_like_its_email(base: str) -> None:
    session = HeadlessSession(base)

    response = session.signup(SIMILAR_EMAIL, password=SIMILAR_PASSWORD)

    assert response.status_code == 400
    assert response.json()["errors"] == [TOO_SIMILAR]
    _assert_nothing_stored()


# The address is used only for the similarity rule


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "email",
    [..., None, "", 42, [SIMILAR_EMAIL], {"email": SIMILAR_EMAIL}, "other@example.org"],
)
def test_without_the_address_the_similarity_rule_does_not_apply(
    base: str, email: object
) -> None:
    session = HeadlessSession(base)

    response = session.password_check(email=email, password=SIMILAR_PASSWORD)

    assert response.status_code == 200
    assert response.json() == {"status": 200}


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "email",
    [
        "  QuillFeather.Robin@Example.org ",
        # Not a valid address: it is never validated, only compared.
        "quillfeather robin",
    ],
)
def test_the_address_is_compared_as_signup_compares_it(base: str, email: str) -> None:
    session = HeadlessSession(base)

    response = session.password_check(email=email, password=SIMILAR_PASSWORD)

    assert response.status_code == 400
    assert response.json()["errors"] == [TOO_SIMILAR]


# A missing or malformed password is a field error


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    ("value", "code"),
    [
        (..., "required"),
        (None, "required"),
        ("", "required"),
        ("   ", "required"),
        (int(MARKER), "invalid"),
        (True, "invalid"),
        ([MARKER], "invalid"),
        ({"x": MARKER}, "invalid"),
    ],
)
def test_a_missing_or_malformed_password_is_a_field_error(
    base: str, value: object, code: str
) -> None:
    session = HeadlessSession(base)

    response = session.password_check(password=value)

    assert response.status_code == 400
    errors = response.json()["errors"]
    assert [(error["param"], error["code"]) for error in errors] == [("password", code)]
    assert MARKER not in response.content.decode()
    assert dict(session.store().items()) == {}
    _assert_nothing_stored()


@pytest.mark.parametrize("base", CLIENTS)
def test_an_empty_body_names_the_password(base: str) -> None:
    session = HeadlessSession(base)

    response = session.post("auth/signup/password-check", {})

    assert response.status_code == 400
    assert [error["param"] for error in response.json()["errors"]] == ["password"]


@pytest.mark.parametrize("base", CLIENTS)
def test_the_missing_password_error_is_signups(base: str) -> None:
    check = HeadlessSession(base).password_check(password=...)

    signup = HeadlessSession(base).signup(SESSION_EMAIL, password="")

    assert signup.status_code == 400
    assert signup.json()["errors"] == check.json()["errors"]


# The check reveals nothing about any email address


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("password", [PASSWORD, "password"])
def test_the_answer_does_not_depend_on_an_account(base: str, password: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    emails: list[object] = [
        ...,
        "taken.player@example.org",
        "TAKEN.player@example.org",
        "new.player@example.org",
        "not an address",
    ]
    user_table = get_user_model()._meta.db_table
    address_table = EmailAddress._meta.db_table
    answers = []
    for email in emails:
        session = HeadlessSession(base)
        with CaptureQueriesContext(connection) as queries:
            response = session.password_check(email=email, password=password)
        answers.append((response.status_code, response.json()))
        sql = " ".join(query["sql"] for query in queries.captured_queries)
        assert user_table not in sql
        assert address_table not in sql
        assert dict(session.store().items()) == {}

    assert all(answer == answers[0] for answer in answers)
    assert EmailAddress.objects.count() == 1
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []


# allauth's answers, the refusal flag, and the signup rate limit


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("password", [PASSWORD, "password"])
def test_a_signed_in_session_gets_409(base: str, password: str) -> None:
    account = UserFactory.create(email=SIGNED_IN, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=SIGNED_IN, verified=True)
    session = HeadlessSession(base)
    login = session.post("auth/login", {"email": SIGNED_IN, "password": PASSWORD})
    assert login.status_code == 200

    response = session.password_check(password=password)

    assert response.status_code == 409


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("password", [PASSWORD, "password"])
def test_a_closed_signup_gets_403(
    base: str, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(AccountAdapter, "is_open_for_signup", lambda *_: False)
    session = HeadlessSession(base)

    response = session.password_check(password=password)

    assert response.status_code == 403


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("password", [PASSWORD, "password", ...])
def test_a_refused_session_gets_signups_refusal(base: str, password: object) -> None:
    session = HeadlessSession(base)
    assert session.age_check(**_young()).status_code == 400
    before = dict(session.store().items())
    assert before == {SIGNUP_REFUSED_SESSION_KEY: True}

    response = session.password_check(password=password)

    assert response.status_code == 400
    assert response.json()["errors"] == REFUSAL
    assert dict(session.store().items()) == before


@pytest.mark.parametrize("base", CLIENTS)
def test_checks_count_against_the_signup_limit(base: str) -> None:
    half = LIMIT // 2
    statuses = [HeadlessSession(base).password_check().status_code for _ in range(half)]
    statuses += [HeadlessSession(base).age_check().status_code for _ in range(half)]

    check = HeadlessSession(base).password_check()
    signup = HeadlessSession(base).signup("late.player@example.org")

    assert statuses == [200] * LIMIT
    assert check.status_code == 429
    assert signup.status_code == 429
    assert _accounts() == 0


# allauth's OpenAPI document lists the check


@pytest.mark.parametrize("document", ["openapi.json", "openapi.yaml"])
def test_the_openapi_document_lists_the_check(document: str) -> None:
    response = Client().get(f"/_allauth/{document}")
    assert response.status_code == 200
    spec = yaml.safe_load(response.content)

    operation: dict[str, Any] = spec["paths"][SPEC_PATH]["post"]

    assert {"$ref": "#/components/parameters/Client"} in operation["parameters"]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body["required"] == ["password"]
    assert set(body["properties"]) == {"email", "password"}
    assert {"200", "400", "403", "409", "429"} <= set(operation["responses"])
    # The age check and allauth's own paths are all still there.
    assert "/_allauth/{client}/v1/auth/signup/age-check" in spec["paths"]
    assert "/_allauth/{client}/v1/auth/signup" in spec["paths"]


@override_settings(HEADLESS_CLIENTS=("app",))
def test_one_client_pins_the_path_like_allauths() -> None:
    spec = open_schema()

    operation = spec["paths"]["/_allauth/app/v1/auth/signup/password-check"]["post"]

    assert "parameters" not in operation
    assert not any("{client}" in path for path in spec["paths"])


def test_the_documented_example_is_a_real_answer() -> None:
    spec = open_schema()
    operation = spec["paths"][SPEC_PATH]["post"]
    examples = operation["responses"]["400"]["content"]["application/json"]["examples"]

    response = HeadlessSession(APP).password_check(password="12345678")

    assert response.json() == examples["password_rules"]["value"]
    assert examples["signup_refused"]["value"]["errors"] == REFUSAL
