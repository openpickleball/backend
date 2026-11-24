"""Accepting the legal agreements at signup, for both clients (#222).

Signup requires ``accepted_terms`` and ``accepted_privacy``, each JSON
``true`` (ADR 0026). They are checked with the age screen, before allauth
reads the email. A new Account gets one acceptance record per agreement,
with the version in settings and the time. The accepted versions are held
in the session with the other signup details. Made-up addresses on
example.org only.
"""

from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import Agreement, AgreementAcceptance
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    SESSION_EMAIL,
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
FIELDS = ["accepted_terms", "accepted_privacy"]
MESSAGES = {
    "accepted_terms": "Accept the Terms of Service.",
    "accepted_privacy": "Accept the Privacy Policy.",
}
TAKEN = "taken.player@example.org"


def _accounts() -> int:
    return get_user_model()._default_manager.count()


def _params(response: Any) -> list[str | None]:
    return [error.get("param") for error in response.json()["errors"]]


def _taken_account() -> None:
    taken = UserFactory.create(email=TAKEN)
    EmailAddress.objects.create(user=taken, email=taken.email, verified=True)


def _assert_nothing_happened(session: HeadlessSession) -> None:
    assert _accounts() == 0
    assert not AgreementAcceptance.objects.exists()
    assert not EmailAddress.objects.exists()
    assert not QueuedEmail.objects.exists()
    assert mail.outbox == []
    stored = session.store()
    assert SIGNUP_REFUSED_SESSION_KEY not in stored
    assert SIGNUP_DETAILS_SESSION_KEY not in stored


# A missing or refused acceptance is a field error


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "value", [..., None, False, "", "true", "on", 1, 0, [True], {"accepted": True}]
)
def test_anything_but_true_is_a_field_error(
    base: str, field: str, value: object
) -> None:
    session = HeadlessSession(base)

    response = session.signup(**{field: value})

    assert response.status_code == 400
    [error] = response.json()["errors"]
    assert error["param"] == field
    assert error["message"] == MESSAGES[field]
    assert error["code"] == "required"
    _assert_nothing_happened(session)


@pytest.mark.parametrize("base", CLIENTS)
def test_each_missing_acceptance_is_reported(base: str) -> None:
    response = HeadlessSession(base).signup(accepted_terms=..., accepted_privacy=False)

    assert response.status_code == 400
    assert sorted(map(str, _params(response))) == FIELDS


@pytest.mark.parametrize("base", CLIENTS)
def test_a_missing_acceptance_with_an_under_13_age_is_not_refused(base: str) -> None:
    session = HeadlessSession(base)
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 6}

    response = session.signup(accepted_terms=False, **young)

    assert response.status_code == 400
    assert _params(response) == ["accepted_terms"]
    assert dict(session.store().items()) == {}


@pytest.mark.parametrize("base", CLIENTS)
def test_a_missing_acceptance_does_not_lock_the_session(base: str) -> None:
    session = HeadlessSession(base)
    session.signup(accepted_privacy=...)

    response = session.signup()

    assert response.status_code == 401
    assert AgreementAcceptance.objects.count() == 2


@pytest.mark.parametrize("base", CLIENTS)
def test_an_acceptance_error_answers_alike_for_new_and_taken_addresses(
    base: str,
) -> None:
    _taken_account()

    new = HeadlessSession(base).signup("new.player@example.org", accepted_terms=False)
    existing = HeadlessSession(base).signup(TAKEN, accepted_terms=False)

    assert new.status_code == existing.status_code == 400
    assert new.json() == existing.json()
    assert mail.outbox == []


# A new Account records what it accepted


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_records_each_agreement_with_its_version_and_time(
    base: str, settings: Settings
) -> None:
    settings.TERMS_OF_SERVICE_VERSION = 4
    settings.PRIVACY_POLICY_VERSION = 9
    before = timezone.now()

    response = HeadlessSession(base).signup()

    assert response.status_code == 401
    account = get_user_model()._default_manager.get_by_natural_key(SESSION_EMAIL)
    rows = AgreementAcceptance.objects.filter(account=account)
    assert {(row.agreement, row.version) for row in rows} == {
        (Agreement.TERMS_OF_SERVICE, 4),
        (Agreement.PRIVACY_POLICY, 9),
    }
    for row in rows:
        assert before <= row.accepted_at <= timezone.now()


@pytest.mark.parametrize("base", CLIENTS)
def test_a_taken_address_records_nothing(base: str) -> None:
    _taken_account()

    response = HeadlessSession(base).signup(TAKEN)

    assert response.status_code == 401
    assert not AgreementAcceptance.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_an_under_13_signup_records_and_holds_nothing(base: str) -> None:
    session = HeadlessSession(base)
    young = {"birth_year": age_limit_date().year - 5, "birth_month": 6}

    response = session.signup(**young)

    assert [error["code"] for error in response.json()["errors"]] == ["signup_refused"]
    assert not AgreementAcceptance.objects.exists()
    stored = session.store()
    assert SIGNUP_DETAILS_SESSION_KEY not in stored
    assert stored[SIGNUP_REFUSED_SESSION_KEY] is True


# The session holds the accepted versions


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_holds_the_accepted_versions(base: str, settings: Settings) -> None:
    settings.TERMS_OF_SERVICE_VERSION = 3
    settings.PRIVACY_POLICY_VERSION = 5
    session = HeadlessSession(base)

    session.signup()

    held = session.store()[SIGNUP_DETAILS_SESSION_KEY]
    assert held["agreements"] == {"terms_of_service": 3, "privacy_policy": 5}


@pytest.mark.parametrize("base", CLIENTS)
def test_new_and_taken_addresses_hold_alike(base: str) -> None:
    _taken_account()
    new_session = HeadlessSession(base)
    taken_session = HeadlessSession(base)

    new = new_session.signup("new.player@example.org")
    existing = taken_session.signup(TAKEN)

    assert new.status_code == existing.status_code == 401
    assert sorted(new.json()) == sorted(existing.json())
    new_held = dict(new_session.store()[SIGNUP_DETAILS_SESSION_KEY])
    taken_held = dict(taken_session.store()[SIGNUP_DETAILS_SESSION_KEY])
    assert new_held.pop("account") is not None
    assert taken_held.pop("account") is None
    assert new_held == taken_held


# The record itself


def test_an_account_accepts_each_version_of_an_agreement_once() -> None:
    account = UserFactory.create()
    now = timezone.now()
    AgreementAcceptance.objects.create(
        account=account, agreement=Agreement.PRIVACY_POLICY, version=2, accepted_at=now
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AgreementAcceptance.objects.create(
            account=account,
            agreement=Agreement.PRIVACY_POLICY,
            version=2,
            accepted_at=now + timedelta(seconds=1),
        )


@pytest.mark.parametrize("version", [0, -1])
def test_a_version_starts_at_1(version: int) -> None:
    account = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        AgreementAcceptance.objects.create(
            account=account,
            agreement=Agreement.TERMS_OF_SERVICE,
            version=version,
            accepted_at=timezone.now(),
        )


def test_an_unknown_agreement_is_refused_by_the_database() -> None:
    account = UserFactory.create()

    with pytest.raises(IntegrityError), transaction.atomic():
        AgreementAcceptance.objects.create(
            account=account, agreement="cookies", version=1, accepted_at=timezone.now()
        )


def test_str_holds_no_email() -> None:
    account = UserFactory.create(email="str.player@example.org")
    row = AgreementAcceptance.objects.create(
        account=account,
        agreement=Agreement.TERMS_OF_SERVICE,
        version=1,
        accepted_at=timezone.now(),
    )

    assert "str.player" not in str(row)
    assert str(row) == "terms_of_service version 1"


# allauth's OpenAPI document lists the fields


def test_spec_lists_both_acceptances_as_required_booleans() -> None:
    spec = Client().get("/_allauth/openapi.json").json()
    base_signup = spec["components"]["schemas"]["BaseSignup"]

    for field in FIELDS:
        assert base_signup["properties"][field] == {"type": "boolean"}
        assert field in base_signup["required"]
