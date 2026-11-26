"""Signing in to the admin and the rules on its sessions (issue #101).

The admin login page takes no password: it signs in through allauth's
headless browser client (password, then a TOTP code), and confirms a
code through ``auth/2fa/reauthenticate`` when the session needs one. The
admin opens only to active staff with MFA whose session passed MFA
within ``ADMIN_SESSION_SECONDS``, and a write needs an MFA step within
``ADMIN_REAUTHENTICATION_SECONDS``. A signed-in Account that is not
active staff gets 404 on every admin URL. Made-up data only.
"""

import re
from typing import Any

import pytest
from django.contrib.auth.models import Group
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.admin import (
    age_mfa_steps,
    sign_in_to_admin,
    staff_with_totp,
    verified_account,
)
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import BROWSER, PASSWORD, csrf_header, post
from apps.audit.models import AuditEvent
from config.admin import MFA_REQUIRED_MESSAGE

pytestmark = pytest.mark.django_db

UNKNOWN = "/no-such-page-x7/"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    # The reauthentication uses the next period's code: allauth refuses
    # a code already used.
    settings.MFA_TOTP_TOLERANCE = 1


def _index() -> str:
    return reverse("admin:index")


def _login_page() -> str:
    return reverse("admin:login")


def _mode(response: Any) -> str | None:
    match = re.search(r'data-mode="([a-z]+)"', response.content.decode())
    return match.group(1) if match else None


def _next(response: Any) -> str | None:
    match = re.search(r'data-next="([^"]*)"', response.content.decode())
    return match.group(1) if match else None


def _admin_pages() -> list[str]:
    account = UserFactory.create()
    return [
        _index(),
        reverse("admin:accounts_user_changelist"),
        reverse("admin:accounts_user_change", args=[account.pk]),
        reverse("admin:password_change"),
        f"{_index()}no-such-model/",
    ]


# Signing in


def test_signing_in_with_password_and_code_opens_the_admin() -> None:
    staff, secret = staff_with_totp(is_superuser=True)

    session = sign_in_to_admin(staff, secret)

    for page in _admin_pages()[:4]:
        assert session.client.get(page).status_code == 200, page


def test_the_login_page_takes_no_password_post(client: Client) -> None:
    staff, _ = staff_with_totp()

    response = client.post(
        _login_page(), {"username": staff.email, "password": PASSWORD}
    )

    assert response.status_code == 405
    assert "_auth_user_id" not in client.session


def test_the_login_page_refuses_a_post_without_a_csrf_token() -> None:
    client = Client(enforce_csrf_checks=True)

    assert client.post(_login_page(), {}).status_code == 405


def test_an_anonymous_request_gets_the_login_page(client: Client) -> None:
    redirected = client.get(_index())
    page = client.get(_login_page())

    assert redirected.status_code == 302
    assert redirected["Location"].startswith(_login_page())
    assert page.status_code == 200
    assert _mode(page) == "login"
    assert _next(page) == _index()


def test_the_login_page_loads_its_script_and_no_inline_script(
    client: Client,
) -> None:
    content = client.get(_login_page()).content.decode()

    assert re.search(
        r'<script type="module" src="[^"]*accounts/admin_login\.js"', content
    )
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", content, re.IGNORECASE)
    assert "style=" not in content
    assert 'data-base="/_allauth/browser/v1"' in content
    assert 'name="csrfmiddlewaretoken"' in content


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example.org/",
        "//evil.example.org/",
        "javascript:alert(1)",
        "http://testserver/x/",
    ],
)
def test_next_must_stay_on_this_site(client: Client, target: str) -> None:
    response = client.get(_login_page(), {"next": target})

    assert _next(response) == _index()


def test_next_on_this_site_is_kept(client: Client) -> None:
    target = reverse("admin:accounts_user_changelist")

    response = client.get(_login_page(), {"next": target})

    assert _next(response) == target


def test_a_signed_in_staff_session_is_sent_on_from_the_login_page() -> None:
    staff, secret = staff_with_totp()
    session = sign_in_to_admin(staff, secret)
    target = reverse("admin:accounts_user_changelist")

    response = session.client.get(_login_page(), {"next": target})

    assert response.status_code == 302
    assert response["Location"] == target


# Sessions that have not passed MFA, or passed it too long ago


def test_a_password_only_session_gets_the_code_page(client: Client) -> None:
    staff, _ = staff_with_totp(is_superuser=True)
    assert client.login(username=staff.email, password=PASSWORD)

    for page in _admin_pages():
        response = client.get(page)
        assert response.status_code == 302, page
        assert response["Location"].startswith(_login_page()), page
    login_page = client.get(_login_page())
    assert login_page.status_code == 200
    assert _mode(login_page) == "code"


def test_an_expired_session_gets_the_code_page(client: Client, settings: Any) -> None:
    staff, _ = staff_with_totp(is_superuser=True)
    client.force_login(staff)
    assert client.get(_index()).status_code == 200

    age_mfa_steps(client.session, settings.ADMIN_SESSION_SECONDS + 5)

    response = client.get(_index())
    assert response.status_code == 302
    assert response["Location"].startswith(_login_page())
    assert _mode(client.get(_login_page())) == "code"


def test_a_session_within_the_limit_keeps_the_admin(
    client: Client, settings: Any
) -> None:
    staff, _ = staff_with_totp(is_superuser=True)
    client.force_login(staff)

    age_mfa_steps(client.session, settings.ADMIN_SESSION_SECONDS - 60)

    assert client.get(_index()).status_code == 200


def test_a_code_reauthentication_reopens_an_expired_session(settings: Any) -> None:
    staff, secret = staff_with_totp(is_superuser=True)
    session = sign_in_to_admin(staff, secret)
    age_mfa_steps(session.client.session, settings.ADMIN_SESSION_SECONDS + 5)
    assert session.client.get(_index()).status_code == 302

    response = session.post("auth/2fa/reauthenticate", {"code": totp_code(secret, 1)})

    assert response.status_code == 200
    assert session.client.get(_index()).status_code == 200


def test_a_password_reauthentication_does_not_open_the_admin(client: Client) -> None:
    staff, _ = staff_with_totp()
    assert client.login(username=staff.email, password=PASSWORD)
    post(
        client,
        f"{BROWSER}/auth/reauthenticate",
        {"password": PASSWORD},
    )

    assert client.get(_index()).status_code == 302


def test_staff_without_mfa_gets_only_the_set_up_page(client: Client) -> None:
    staff = verified_account(is_staff=True, is_superuser=True)
    client.force_login(staff)

    for page in _admin_pages():
        response = client.get(page)
        assert response.status_code == 302, page
        assert response["Location"].startswith(_login_page()), page
    login_page = client.get(_login_page())
    content = login_page.content.decode()
    assert login_page.status_code == 403
    assert MFA_REQUIRED_MESSAGE in content
    assert "accounts/user" not in content
    assert staff.email not in content
    assert "data-mode" not in content


def test_an_inactive_staff_account_is_not_let_in(client: Client) -> None:
    staff, _ = staff_with_totp(is_superuser=True)
    client.force_login(staff)
    staff.is_active = False
    staff.save()

    response = client.get(_index())

    assert response.status_code == 302


# Accounts that are not staff


def test_a_signed_in_player_gets_not_found_on_every_admin_url(client: Client) -> None:
    player = verified_account()
    client.force_login(player)
    unknown = client.get(UNKNOWN).status_code

    for page in [*_admin_pages(), _login_page(), reverse("admin:logout")]:
        assert client.get(page).status_code == unknown == 404, page
    assert client.post(_login_page()).status_code == 404
    assert client.post(reverse("admin:logout")).status_code == 404


def test_a_player_with_mfa_gets_not_found_too() -> None:
    player = verified_account()
    secret = enroll_totp(player).secret
    session = sign_in_to_admin(player, secret)

    assert session.client.get(_index()).status_code == 404
    assert session.client.get(_login_page()).status_code == 404


# Writes need a recent MFA step


def _add_group(client: Client, name: str) -> Any:
    return client.post(reverse("admin:auth_group_add"), {"name": name})


def test_a_stale_write_is_sent_to_the_code_page_and_changes_nothing(
    client: Client, settings: Any
) -> None:
    settings.ADMIN_REAUTHENTICATION_SECONDS = 30
    staff, _ = staff_with_totp(is_superuser=True)
    client.force_login(staff)
    age_mfa_steps(client.session, 60)
    events = AuditEvent.objects.count()

    response = _add_group(client, "made-up-group")

    assert response.status_code == 302
    path = reverse("admin:auth_group_add")
    assert response["Location"] == f"{_login_page()}?next={path}"
    assert not Group.objects.filter(name="made-up-group").exists()
    assert AuditEvent.objects.count() == events
    # Reading still works within the session limit.
    assert client.get(_index()).status_code == 200
    assert _mode(client.get(_login_page())) == "code"


def test_a_write_succeeds_after_a_code_reauthentication(settings: Any) -> None:
    settings.ADMIN_REAUTHENTICATION_SECONDS = 30
    staff, secret = staff_with_totp(is_superuser=True)
    session = sign_in_to_admin(staff, secret)
    age_mfa_steps(session.client.session, 60)
    stale = session.client.post(
        reverse("admin:auth_group_add"),
        {"name": "made-up-group"},
        headers=csrf_header(session.client),
    )
    assert stale.status_code == 302
    assert stale["Location"].startswith(_login_page())
    assert not Group.objects.filter(name="made-up-group").exists()

    reauthenticated = session.post(
        "auth/2fa/reauthenticate", {"code": totp_code(secret, 1)}
    )
    response = session.client.post(
        reverse("admin:auth_group_add"),
        {"name": "made-up-group"},
        headers=csrf_header(session.client),
    )

    assert reauthenticated.status_code == 200
    assert response.status_code == 302
    assert Group.objects.filter(name="made-up-group").exists()


def test_a_fresh_write_goes_through(client: Client) -> None:
    staff, _ = staff_with_totp(is_superuser=True)
    client.force_login(staff)

    assert _add_group(client, "made-up-group").status_code == 302
    assert Group.objects.filter(name="made-up-group").exists()


def test_logging_out_needs_no_fresh_step(client: Client, settings: Any) -> None:
    settings.ADMIN_REAUTHENTICATION_SECONDS = 30
    staff, _ = staff_with_totp()
    client.force_login(staff)
    age_mfa_steps(client.session, 60)

    response = client.post(reverse("admin:logout"))

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session
