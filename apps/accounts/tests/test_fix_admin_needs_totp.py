"""The admin needs TOTP, not just any method (issue #117, ADR 0056).

The admin's login script takes only codes, so a staff Account whose only
method is a passkey would spend its recovery codes. Such an Account gets
the page asking it to set up two-factor authentication, cannot use the
admin even after a passkey login, and cannot be granted staff or
superuser status. Made-up data only."""

from typing import TYPE_CHECKING, Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.admin import GRANT_NEEDS_MFA_MESSAGE
from apps.accounts.mfa import has_mfa, session_passed_mfa
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    LOGIN_PATH,
    added,
    new_passkey,
    request_options,
    use_passkey_settings,
    verified_account,
)
from config.admin import MFA_REQUIRED_MESSAGE

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _passkey_only(**fields: Any) -> tuple["_User", HeadlessSession]:
    """An Account whose only method is a passkey, and a session that
    signed in with it."""
    account = verified_account(mfa=False, **fields)
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    assert has_mfa(account)
    session = HeadlessSession(BROWSER)
    credential = passkey.sign_in(request_options(session, LOGIN_PATH))
    assert session.post(LOGIN_PATH, {"credential": credential}).status_code == 200
    assert session_passed_mfa(session.store(), account)
    return account, session


def test_passkey_only_staff_gets_the_set_up_page() -> None:
    staff, session = _passkey_only(is_staff=True, is_superuser=True)

    page = session.client.get("/admin/login/")

    assert page.status_code == 403
    assert MFA_REQUIRED_MESSAGE in page.content.decode()
    assert staff.email not in page.content.decode()


def test_passkey_only_staff_cannot_open_the_admin() -> None:
    _, session = _passkey_only(is_staff=True, is_superuser=True)

    response = session.client.get("/admin/")

    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")


@pytest.mark.parametrize("field", ["is_staff", "is_superuser"])
def test_staff_status_is_not_granted_to_a_passkey_only_account(
    client: Client, field: str
) -> None:
    account, _ = _passkey_only()
    client.force_login(UserFactory.create(is_staff=True, is_superuser=True))

    response = client.post(
        reverse("admin:accounts_user_change", args=[account.pk]),
        {"email": account.email, "is_active": "on", field: "on"},
    )

    assert response.status_code == 200
    form = response.context["adminform"].form
    assert GRANT_NEEDS_MFA_MESSAGE in [str(error) for error in form.errors[field]]
    account.refresh_from_db()
    assert not getattr(account, field)
