"""The name and place on allauth's headless signup, for both clients (#221).

Signup asks for a first and last name, required, and a city and region,
optional (ADR 0022). They are checked with the age screen, before allauth
reads the email, and held in the session with the age and gender division:
"First Last" as the legal name, which is also the starting display name.
Made-up names and addresses on example.org only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    SESSION_EMAIL,
    SIGNUP_FULL_NAME,
    SIGNUP_LOCATION,
    HeadlessSession,
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


def _accounts() -> int:
    return get_user_model()._default_manager.count()


def _params(response: Any) -> list[str | None]:
    return [error.get("param") for error in response.json()["errors"]]


def _assert_nothing_happened(session: HeadlessSession) -> None:
    assert _accounts() == 0
    assert not EmailAddress.objects.exists()
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []
    stored = session.store()
    assert SIGNUP_REFUSED_SESSION_KEY not in stored
    assert SIGNUP_DETAILS_SESSION_KEY not in stored


# A missing or malformed value is a field error


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("field", ["first_name", "last_name"])
@pytest.mark.parametrize(
    "value",
    [
        ...,
        None,
        "",
        "   ",
        "ㅤ",
        f"{MARKER}\x00",
        f"{MARKER}\ud800",
        int(MARKER),
        [MARKER],
        {"name": MARKER},
    ],
)
def test_a_missing_or_malformed_name_is_a_field_error(
    base: str, field: str, value: object
) -> None:
    session = HeadlessSession(base)

    response = session.signup(**{field: value})

    assert response.status_code == 400
    assert _params(response) == [field]
    assert MARKER not in response.content.decode()
    _assert_nothing_happened(session)


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("field", ["city", "region"])
@pytest.mark.parametrize(
    "value",
    ["x" * 101, f"{MARKER}\x07", f"{MARKER}\udfff", int(MARKER), [MARKER]],
)
def test_a_malformed_place_is_a_field_error(
    base: str, field: str, value: object
) -> None:
    session = HeadlessSession(base)

    response = session.signup(**{field: value})

    assert response.status_code == 400
    assert _params(response) == [field]
    assert MARKER not in response.content.decode()
    _assert_nothing_happened(session)


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("first_name", ..., "Enter your first name."),
        ("first_name", "  ", "Enter your first name."),
        ("last_name", ..., "Enter your last name."),
        ("last_name", "ㅤ", "Enter your last name."),
        ("first_name", 7, "Enter text."),
        ("last_name", "A\x00", "Remove control or invalid characters."),
        ("city", "x" * 101, "Enter at most 100 characters."),
        ("region", "A\x1fB", "Remove control or invalid characters."),
    ],
)
def test_name_and_place_errors_have_fixed_messages(
    base: str, field: str, value: object, message: str
) -> None:
    response = HeadlessSession(base).signup(**{field: value})

    [error] = response.json()["errors"]
    assert error["param"] == field
    assert error["message"] == message


@pytest.mark.parametrize("base", CLIENTS)
def test_a_full_name_too_long_for_a_display_name_is_an_error_on_last_name(
    base: str,
) -> None:
    session = HeadlessSession(base)

    # "First Last" is 65 characters: one over the display name's limit.
    response = session.signup(first_name="A" * 32, last_name="B" * 32)

    assert response.status_code == 400
    [error] = response.json()["errors"]
    assert error["param"] == "last_name"
    assert error["message"] == (
        "Your first and last name together can be at most 63 characters."
    )
    _assert_nothing_happened(session)


@pytest.mark.parametrize("base", CLIENTS)
def test_a_full_name_at_the_display_name_limit_signs_up(base: str) -> None:
    session = HeadlessSession(base)

    response = session.signup(first_name="A" * 31, last_name="B" * 32)

    assert response.status_code == 401
    held = session.store()[SIGNUP_DETAILS_SESSION_KEY]
    assert held["legal_name"] == f"{'A' * 31} {'B' * 32}"


@pytest.mark.parametrize("base", CLIENTS)
def test_each_name_error_is_reported(base: str) -> None:
    response = HeadlessSession(base).signup(first_name=..., last_name="")

    assert response.status_code == 400
    assert sorted(map(str, _params(response))) == ["first_name", "last_name"]


@pytest.mark.parametrize("base", CLIENTS)
def test_a_malformed_name_does_not_lock_the_session(base: str) -> None:
    session = HeadlessSession(base)
    session.signup(first_name="")

    response = session.signup()

    assert response.status_code == 401
    assert _accounts() == 1


@pytest.mark.parametrize("base", CLIENTS)
def test_a_missing_name_with_an_under_13_age_is_not_refused(base: str) -> None:
    session = HeadlessSession(base)
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 6}

    response = session.signup(last_name=..., **young)

    assert response.status_code == 400
    assert _params(response) == ["last_name"]
    assert dict(session.store().items()) == {}


@pytest.mark.parametrize("base", CLIENTS)
def test_an_under_13_signup_with_valid_names_holds_nothing(base: str) -> None:
    session = HeadlessSession(base)
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 6}

    response = session.signup(**young)

    assert response.status_code == 400
    assert [error["code"] for error in response.json()["errors"]] == ["signup_refused"]
    stored = session.store()
    assert SIGNUP_DETAILS_SESSION_KEY not in stored
    assert stored[SIGNUP_REFUSED_SESSION_KEY] is True
    assert SIGNUP_FULL_NAME not in str(dict(stored.items()))


# A valid signup holds the name and place


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_holds_first_last_as_the_legal_name_with_the_place(
    base: str,
) -> None:
    session = HeadlessSession(base)

    response = session.signup(
        first_name="  Robin ", last_name=" Quillfeather  ", city=" Lakeview "
    )

    assert response.status_code == 401
    account = get_user_model()._default_manager.get_by_natural_key(SESSION_EMAIL)
    held = session.store()[SIGNUP_DETAILS_SESSION_KEY]
    assert held["account"] == account.pk
    assert held["legal_name"] == SIGNUP_FULL_NAME
    assert (held["city"], held["region"]) == ("Lakeview", SIGNUP_LOCATION["region"])


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("value", [..., None, "", "   "])
def test_the_city_and_region_are_optional(base: str, value: object) -> None:
    session = HeadlessSession(base)

    response = session.signup(city=value, region=value)

    assert response.status_code == 401
    held = session.store()[SIGNUP_DETAILS_SESSION_KEY]
    assert (held["city"], held["region"]) == ("", "")
    assert held["legal_name"] == SIGNUP_FULL_NAME


@pytest.mark.parametrize("base", CLIENTS)
def test_a_taken_address_holds_the_names_with_no_account(base: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    session = HeadlessSession(base)

    response = session.signup("taken.player@example.org")

    assert response.status_code == 401
    held = session.store()[SIGNUP_DETAILS_SESSION_KEY]
    assert held["account"] is None
    assert held["legal_name"] == SIGNUP_FULL_NAME
    assert (held["city"], held["region"]) == (
        SIGNUP_LOCATION["city"],
        SIGNUP_LOCATION["region"],
    )


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
@pytest.mark.parametrize("place", [{}, {"city": ..., "region": ...}, {"region": ""}])
def test_new_and_taken_addresses_answer_alike(
    base: str, place: dict[str, object]
) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)
    new_session = HeadlessSession(base)
    taken_session = HeadlessSession(base)

    new = new_session.signup("new.player@example.org", **place)
    existing = taken_session.signup("taken.player@example.org", **place)

    assert _outline(new) == _outline(existing)
    new_held = dict(new_session.store()[SIGNUP_DETAILS_SESSION_KEY])
    taken_held = dict(taken_session.store()[SIGNUP_DETAILS_SESSION_KEY])
    assert sorted(new_held) == sorted(taken_held)
    new_held.pop("account")
    taken_held.pop("account")
    assert new_held == taken_held


@pytest.mark.parametrize("base", CLIENTS)
def test_a_name_error_answers_alike_for_new_and_taken_addresses(base: str) -> None:
    taken = UserFactory.create(email="taken.player@example.org")
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)

    new = HeadlessSession(base).signup("new.player@example.org", first_name="")
    existing = HeadlessSession(base).signup("taken.player@example.org", first_name="")

    assert new.status_code == existing.status_code == 400
    assert new.json() == existing.json()
    assert mail.outbox == []


# allauth's OpenAPI document lists the fields


def test_spec_lists_the_names_as_required_and_the_place_as_optional() -> None:
    spec = Client().get("/_allauth/openapi.json").json()
    base_signup = spec["components"]["schemas"]["BaseSignup"]

    assert {"first_name", "last_name", "city", "region"} <= set(
        base_signup["properties"]
    )
    assert {"first_name", "last_name"} <= set(base_signup["required"])
    assert not {"city", "region"} & set(base_signup["required"])
