"""The new sign-in email (#100, ADR 0047).

A completed login of an Account that had signed in before, from a
browser or app whose device marker does not show this Account, queues one
email from an on_commit callback: nothing is sent in the request. The
browser keeps a signed cookie, the app a signed token in a header, each a
short list of keyed hashes of Account ids. An Account's first sign-in
sends nothing. Made-up data only.
"""

import re
from pathlib import Path
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.core import mail, signing
from django.core.mail import EmailMultiAlternatives

from apps.accounts.devices import (
    DEVICE_TOKEN_HEADER,
    NEW_SIGN_IN,
    account_tag,
    read_tags,
    render_sign_in_alert,
)
from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.emails.models import QueuedEmail
from apps.emails.tests.hygiene import (
    assert_clean_message,
    assert_clean_template,
    assert_links_only_to_the_web_app,
)

pytestmark = pytest.mark.django_db

EMAIL = "alert.player@example.org"
COOKIE = settings.SIGN_IN_DEVICE_COOKIE_NAME
SESSIONS_URL = f"{settings.FRONTEND_URL}/account/sessions"
USER_AGENT = "Made-up Browser/7.0"
IP = "203.0.113.42"
TEMPLATES = Path(settings.BASE_DIR) / "templates" / "accounts" / "email"


def _account(email: str = EMAIL) -> Any:
    account = UserFactory.create(email=email, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=email, primary=True, verified=True)
    return account


def _alerts() -> list[QueuedEmail]:
    """Alerts queued and not sent yet (a sent row is deleted)."""
    return list(QueuedEmail.objects.filter(template=NEW_SIGN_IN))


def _sent_alerts() -> list[Any]:
    return [m for m in mail.outbox if "New sign-in" in str(m.subject)]


class App:
    """An app install: a fresh session each login, keeping its device token."""

    def __init__(self) -> None:
        self.device_token: str | None = None

    def login(self, email: str = EMAIL) -> Any:
        session = HeadlessSession(APP)
        headers = {DEVICE_TOKEN_HEADER: self.device_token} if self.device_token else {}
        response = session.client.post(
            f"{APP}/auth/login",
            {"email": email, "password": PASSWORD},
            content_type="application/json",
            headers=headers,
        )
        token = response.headers.get(DEVICE_TOKEN_HEADER)
        if token:
            self.device_token = token
        return response


class Browser:
    """A browser: one cookie jar, logging in and out."""

    def __init__(self) -> None:
        self.session = HeadlessSession(BROWSER)

    def login(self, email: str = EMAIL) -> Any:
        response = self.session.client.post(
            f"{BROWSER}/auth/login",
            {"email": email, "password": PASSWORD},
            content_type="application/json",
            headers={
                **self.session._headers(),
                "User-Agent": USER_AGENT,
                settings.CLIENT_IP_HEADER: IP,
            },
        )
        if response.status_code == 200:
            self.session.logout()
        return response


def test_the_first_sign_in_ever_sends_nothing() -> None:
    _account()

    response = Browser().login()

    assert response.status_code == 200
    assert _sent_alerts() == []
    assert COOKIE in response.cookies


def test_a_new_browser_gets_one_alert_and_a_known_one_none() -> None:
    _account()
    first = Browser()
    first.login()
    assert first.login().status_code == 200
    assert _sent_alerts() == []

    second = Browser()
    assert second.login().status_code == 200
    assert len(_sent_alerts()) == 1

    assert second.login().status_code == 200
    assert first.login().status_code == 200
    assert len(_sent_alerts()) == 1


@pytest.mark.hold_on_commit
def test_the_alert_is_queued_after_commit_not_in_the_request(
    django_capture_on_commit_callbacks: Any,
) -> None:
    account = _account()
    account.last_login = account.date_joined
    account.save(update_fields=["last_login"])

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        response = Browser().login()
        assert response.status_code == 200
        assert _alerts() == []
        assert _sent_alerts() == []

    assert callbacks
    for callback in callbacks:
        callback()
    assert len(_alerts()) == 1


def test_the_app_token_round_trip() -> None:
    _account()
    app = App()
    first = app.login()
    assert first.status_code == 200
    token = first.headers[DEVICE_TOKEN_HEADER]
    assert app.login().status_code == 200
    assert _sent_alerts() == []

    assert App().login().status_code == 200
    assert len(_sent_alerts()) == 1
    # The same install, its token refreshed at each login.
    assert app.device_token
    assert app.login().status_code == 200
    assert len(_sent_alerts()) == 1
    assert token


def test_a_tampered_or_foreign_token_counts_as_a_new_device() -> None:
    _account()
    _account("other.alert@example.org")
    App().login()
    foreign = App()
    foreign.login("other.alert@example.org")
    tampered = App()
    tampered.device_token = "not-a-signed-token"

    foreign.login(EMAIL)
    tampered.login(EMAIL)

    assert len(_sent_alerts()) == 2


def test_the_marker_holds_keyed_hashes_only() -> None:
    account = _account()
    response = Browser().login()

    value = response.cookies[COOKIE].value
    assert EMAIL not in value
    tags = read_tags(value)
    assert tags == [account_tag(account)]
    assert str(account.pk) not in tags
    assert re.fullmatch(r"[0-9a-f]{32}", tags[0])
    # Signed: a changed value reads as no marker.
    assert read_tags(value + "x") == []
    assert read_tags(signing.dumps([tags[0]], salt="another-salt")) == []


def test_the_cookie_attributes() -> None:
    _account()
    response = Browser().login()

    morsel = response.cookies[COOKIE]
    assert morsel["httponly"] is True
    assert morsel["samesite"] == "Lax"
    assert morsel["path"] == "/"
    assert not morsel["domain"]
    assert int(morsel["max-age"]) == settings.SIGN_IN_DEVICE_COOKIE_DAYS * 86400
    assert bool(morsel["secure"]) is settings.SIGN_IN_DEVICE_COOKIE_SECURE


def test_the_marker_is_capped() -> None:
    browser = Browser()
    accounts = [
        _account(f"cap{n}.player@example.org")
        for n in range(settings.SIGN_IN_DEVICE_MAX_TAGS + 3)
    ]
    response = None
    for account in accounts:
        response = browser.login(account.email)
    assert response is not None

    tags = read_tags(browser.session.client.cookies[COOKIE].value)
    assert len(tags) == settings.SIGN_IN_DEVICE_MAX_TAGS == 10
    assert tags[0] == account_tag(accounts[-1])


def test_a_failed_login_sends_nothing_and_sets_no_marker() -> None:
    account = _account()
    account.last_login = account.date_joined
    account.save(update_fields=["last_login"])
    session = HeadlessSession(BROWSER)

    response = session.login(EMAIL, "not-the-password")

    assert response.status_code == 400
    assert COOKIE not in response.cookies
    assert _sent_alerts() == []


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_an_mfa_login_alerts_once_after_the_code(base: str, settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1
    account = _account()
    account.last_login = account.date_joined
    account.save(update_fields=["last_login"])
    secret = enroll_totp(account).secret
    session = HeadlessSession(base)

    stage = session.login(EMAIL)
    assert stage.status_code == 401
    assert _sent_alerts() == []
    done = session.post("auth/2fa/authenticate", {"code": totp_code(secret)})

    assert done.status_code == 200
    assert len(_sent_alerts()) == 1
    if base == APP:
        assert done.headers[DEVICE_TOKEN_HEADER]
    else:
        assert COOKIE in done.cookies


def test_the_email_names_time_and_client_and_nothing_else() -> None:
    _account()
    Browser().login()
    Browser().login()

    [message] = _sent_alerts()
    assert message.to == [EMAIL]
    assert message.subject == "New sign-in to your OPEN account"
    body = str(message.body)
    assert "a web browser" in body
    assert re.search(r"\d{1,2} \w+ \d{4} at \d{2}:\d{2} UTC", body)
    assert SESSIONS_URL in body
    for leaked in (IP, USER_AGENT, "Made-up Browser", "testserver", EMAIL):
        assert leaked not in body
        assert all(leaked not in str(c) for c, _ in message.alternatives)
    assert isinstance(message, EmailMultiAlternatives)
    assert_clean_message(message)
    assert_links_only_to_the_web_app(str(message.alternatives[0][0]))


def test_the_app_client_is_named() -> None:
    account = _account()

    message = render_sign_in_alert(account, client="app", at=account.date_joined)

    assert "the OPEN app" in str(message.body)
    assert_clean_message(message)


def test_the_templates_are_clean() -> None:
    files = sorted(TEMPLATES.glob("new_sign_in_*"))

    assert [f.name for f in files] == [
        "new_sign_in_message.html",
        "new_sign_in_message.txt",
        "new_sign_in_subject.txt",
    ]
    for path in files:
        assert_clean_template(path)


def test_an_inactive_account_gets_no_alert() -> None:
    account = _account()
    account.last_login = account.date_joined
    account.save(update_fields=["last_login"])
    browser = Browser()
    browser.login()
    assert len(_sent_alerts()) == 1
    # Deactivated between the login and the callback: nothing more.
    from apps.accounts.devices import send_sign_in_alert

    account.is_active = False
    account.save(update_fields=["is_active"])
    send_sign_in_alert(account.pk, "browser", account.date_joined.isoformat())

    assert len(_sent_alerts()) == 1


def test_cors_allows_and_exposes_the_device_token_header() -> None:
    assert DEVICE_TOKEN_HEADER.lower() in settings.CORS_ALLOW_HEADERS
    assert DEVICE_TOKEN_HEADER in settings.CORS_EXPOSE_HEADERS
