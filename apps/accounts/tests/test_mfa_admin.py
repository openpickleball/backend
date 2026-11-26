"""The admin needs MFA for every staff Account (issue #99, ADR 0043)."""

import pytest
from allauth.mfa.models import Authenticator
from django.contrib import admin
from django.test import Client

from apps.accounts.tests.admin import (
    sign_in_to_admin,
    staff_with_totp,
    verified_account,
)
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.accounts.tests.headless import BROWSER, HeadlessSession
from config.admin import MFA_REQUIRED_MESSAGE, OpenAdminSite

pytestmark = pytest.mark.django_db

ADMIN_LOGIN = "/admin/login/"


def _pages(client: Client) -> list[str]:
    account = UserFactory.create()
    return [
        "/admin/",
        "/admin/accounts/user/",
        f"/admin/accounts/user/{account.pk}/change/",
    ]


def test_the_admin_site_is_open_admin_site() -> None:
    assert isinstance(admin.site, OpenAdminSite)


def test_allauths_authenticator_admin_is_not_registered() -> None:
    assert not admin.site.is_registered(Authenticator)


def test_staff_without_mfa_gets_the_login_page_on_every_admin_page(
    client: Client,
) -> None:
    staff = UserFactory.create(is_staff=True, is_superuser=True, mfa=False)
    client.force_login(staff)

    for page in _pages(client):
        response = client.get(page)
        assert response.status_code == 302, page
        assert response["Location"].startswith(ADMIN_LOGIN), page


def test_staff_with_mfa_reaches_every_admin_page(client: Client) -> None:
    staff = UserFactory.create(is_staff=True, is_superuser=True, mfa=False)
    enroll_totp(staff)
    client.force_login(staff)

    for page in _pages(client):
        assert client.get(page).status_code == 200, page


def test_staff_without_mfa_cannot_use_the_admin_after_signing_in() -> None:
    staff = verified_account(is_staff=True)
    session = HeadlessSession(BROWSER)

    # allauth signs the Account in: it has no second factor to ask for.
    assert session.login(staff.email).status_code == 200
    page = session.client.get(ADMIN_LOGIN)

    assert page.status_code == 403
    assert MFA_REQUIRED_MESSAGE in page.content.decode()
    assert staff.email not in page.content.decode()
    assert session.client.get("/admin/").status_code == 302


def test_staff_with_mfa_logs_in_to_the_admin() -> None:
    staff, secret = staff_with_totp()

    session = sign_in_to_admin(staff, secret)

    assert session.client.session["_auth_user_id"] == str(staff.pk)
    assert session.client.get("/admin/").status_code == 200


def test_a_wrong_password_signs_nobody_in() -> None:
    staff, _ = staff_with_totp()
    session = HeadlessSession(BROWSER)

    response = session.login(staff.email, "not-the-password")

    assert response.status_code == 400
    assert MFA_REQUIRED_MESSAGE not in response.content.decode()
    assert "_auth_user_id" not in session.client.session
    assert session.client.get("/admin/").status_code == 302


def test_recovery_codes_alone_do_not_open_the_admin(client: Client) -> None:
    from allauth.mfa.recovery_codes.internal.auth import RecoveryCodes

    staff = UserFactory.create(is_staff=True, is_superuser=True, mfa=False)
    RecoveryCodes.activate(staff)
    client.force_login(staff)

    assert client.get("/admin/").status_code == 302
