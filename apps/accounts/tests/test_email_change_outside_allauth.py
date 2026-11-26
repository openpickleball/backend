"""A change of User.email outside allauth drops the old EmailAddress rows
(issue #30, ADR 0003).

When staff change an Account's address in the admin, or any save()
changes `email`, the Account's EmailAddress rows for other addresses are
deleted in the same transaction, so the old address is free for its real
owner to sign up with and verify. allauth's own change-of-address flow
keeps its new verified primary row. Made-up addresses only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.accounts.tests.headless import (
    APP,
    VERIFY_PREFIX,
    app_headers,
    key_from_link,
    link_in_last_mail,
    login,
    post,
    signup,
    verify_from_last_mail,
)

pytestmark = pytest.mark.django_db

OLD = "old.address@example.org"
NEW = "new.address@example.org"


@pytest.fixture
def account() -> Any:
    """An Account verified through the headless API."""
    signup(Client(), APP, OLD)
    verify_from_last_mail(Client(), APP)
    mail.outbox.clear()
    cache.clear()
    return get_user_model()._default_manager.get_by_natural_key(OLD)


@pytest.fixture
def staff_client() -> Client:
    client = Client()
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))
    return client


def _admin_save(client: Client, account: Any, email: str, **extra: str) -> Any:
    data = {
        "email": email,
        "is_active": "on",
        "date_joined_0": "2026-01-01",
        "date_joined_1": "00:00:00",
        "last_login_0": "",
        "last_login_1": "",
        **extra,
    }
    return client.post(reverse("admin:accounts_user_change", args=[account.pk]), data)


def _rows(account: Any) -> list[tuple[str, bool, bool]]:
    return sorted(
        EmailAddress.objects.filter(user=account).values_list(
            "email", "primary", "verified"
        )
    )


def test_an_admin_email_change_drops_the_old_rows(
    account: Any, staff_client: Client
) -> None:
    response = _admin_save(staff_client, account, NEW)

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.email == NEW
    assert _rows(account) == []


def test_a_save_that_changes_email_drops_the_old_rows(account: Any) -> None:
    account.email = NEW
    account.save()

    assert _rows(account) == []


def test_the_old_address_can_then_sign_up_and_verify(
    account: Any, staff_client: Client
) -> None:
    _admin_save(staff_client, account, NEW)

    signed_up = signup(Client(), APP, OLD)
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)
    verified = post(Client(), f"{APP}/auth/email/verify", {"key": key})

    assert signed_up.status_code == 401
    assert verified.status_code == 401
    owner = get_user_model()._default_manager.get_by_natural_key(OLD)
    assert owner.pk != account.pk
    assert _rows(owner) == [(OLD, True, True)]


def test_an_admin_save_that_keeps_the_email_keeps_the_rows(
    account: Any, staff_client: Client
) -> None:
    before = _rows(account)
    # Staff status goes only to an Account with MFA.
    enroll_totp(account)

    response = _admin_save(staff_client, account, OLD, is_staff="on")

    assert response.status_code == 302
    account.refresh_from_db()
    assert account.is_staff
    assert _rows(account) == before == [(OLD, True, True)]


def test_allauths_change_of_address_keeps_one_verified_primary_row(
    account: Any,
) -> None:
    token = login(Client(), APP, OLD).json()["meta"]["session_token"]
    post(Client(), f"{APP}/account/email", {"email": NEW}, **app_headers(token))
    key = key_from_link(link_in_last_mail(VERIFY_PREFIX), VERIFY_PREFIX)

    verified = post(Client(), f"{APP}/auth/email/verify", {"key": key})

    assert verified.status_code == 401
    account.refresh_from_db()
    assert account.email == NEW
    assert _rows(account) == [(NEW, True, True)]
