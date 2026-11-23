"""Another Account's pending change of address does not block sign-up
(issue #30, ADR 0003, hard rule 8).

An address is taken only when it is some Account's `User.email`. If
Account A has merely added an address and not verified it, a new person
can sign up with that address: the new Account is created unverified and
the verification link goes to the address. A's old key is then refused,
and the new owner's key verifies. Both clients.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    SIGNUP_DETAILS,
    VERIFY_PREFIX,
    app_headers,
    browser_client,
    csrf_header,
    key_from_link,
    link_in_last_mail,
    login,
    post,
    signup,
    verify_from_last_mail,
)

pytestmark = pytest.mark.django_db

A_EMAIL = "first.player@example.org"
X_EMAIL = "new.player@example.org"
CLIENTS = [APP, BROWSER]


def _post(base: str, endpoint: str, data: dict[str, Any]) -> Any:
    if base == APP:
        return post(Client(), f"{APP}/{endpoint}", data)
    client = browser_client()
    return post(client, f"{BROWSER}/{endpoint}", data, **csrf_header(client))


def _pending_change_key() -> str:
    """Account A adds X_EMAIL and leaves it unverified; A's key for it."""
    signup(Client(), APP, A_EMAIL)
    verify_from_last_mail(Client(), APP)
    token = login(Client(), APP, A_EMAIL).json()["meta"]["session_token"]
    mail.outbox.clear()
    post(Client(), f"{APP}/account/email", {"email": X_EMAIL}, **app_headers(token))
    assert [message.to for message in mail.outbox] == [[X_EMAIL]]
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    mail.outbox.clear()
    # A's mail used up the address's send limit.
    cache.clear()
    return key


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_with_a_pending_change_address_creates_the_account(base: str) -> None:
    _pending_change_key()

    response = _post(
        base, "auth/signup", {"email": X_EMAIL, "password": PASSWORD, **SIGNUP_DETAILS}
    )

    assert response.status_code == 401
    owner = get_user_model()._default_manager.get_by_natural_key(X_EMAIL)
    address = EmailAddress.objects.get(user=owner)
    assert (address.email, address.primary, address.verified) == (X_EMAIL, True, False)
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [X_EMAIL]
    assert link_in_last_mail(VERIFY_PREFIX).startswith(VERIFY_PREFIX)


@pytest.mark.parametrize("base", CLIENTS)
def test_the_old_key_is_refused_and_the_new_owner_verifies(base: str) -> None:
    old_key = _pending_change_key()
    _post(
        base, "auth/signup", {"email": X_EMAIL, "password": PASSWORD, **SIGNUP_DETAILS}
    )
    new_key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)

    refused = _post(base, "auth/email/verify", {"key": old_key})
    verified = _post(base, "auth/email/verify", {"key": new_key})

    assert refused.status_code == 400
    assert [error["code"] for error in refused.json()["errors"]] == ["email_taken"]
    # Verifying by link does not log in: the client logs in next.
    assert verified.status_code == 401
    owner = get_user_model()._default_manager.get_by_natural_key(X_EMAIL)
    address = EmailAddress.objects.get(user=owner)
    assert (address.email, address.primary, address.verified) == (X_EMAIL, True, True)
    account_a = get_user_model()._default_manager.get_by_natural_key(A_EMAIL)
    assert account_a.email == A_EMAIL
    assert EmailAddress.objects.get(user=account_a, primary=True).verified


@pytest.mark.parametrize("base", CLIENTS)
def test_signup_with_a_real_login_stays_enumeration_safe(base: str) -> None:
    signup(Client(), APP, A_EMAIL)
    verify_from_last_mail(Client(), APP)
    mail.outbox.clear()
    cache.clear()

    response = _post(
        base, "auth/signup", {"email": A_EMAIL, "password": PASSWORD, **SIGNUP_DETAILS}
    )

    assert response.status_code == 401
    assert get_user_model()._default_manager.count() == 1
    assert [message.to for message in mail.outbox] == [[A_EMAIL]]
    assert mail.outbox[0].subject == "You already have an OPEN account"
    assert VERIFY_PREFIX not in mail.outbox[0].body
