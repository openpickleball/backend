"""Account emails leave through the email queue (issue #35, ADR 0016).

No allauth flow sends mail during the request. The adapter stores the
rendered message as a QueuedEmail row, and the send task runs once the
transaction commits. These tests hold on_commit callbacks (the
``hold_on_commit`` mark) to see the state when the response returns,
then run them as a commit would. Made-up addresses only.
"""

from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.core.mail import EmailMessage
from django.test import Client
from pytest_django import DjangoCaptureOnCommitCallbacks

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    RESET_PREFIX,
    SIGNUP_DETAILS,
    VERIFY_PREFIX,
    app_headers,
    browser_client,
    csrf_header,
    login,
    post,
    signup,
)
from apps.emails.models import QueuedEmail

pytestmark = [pytest.mark.django_db, pytest.mark.hold_on_commit]

EMAIL = "queue-sam@example.org"
NEW_EMAIL = "queue-sam-new@example.org"


def _client(base: str) -> tuple[Client, dict[str, str]]:
    if base == BROWSER:
        client = browser_client()
        return client, csrf_header(client)
    return Client(), {}


def _verified_account() -> None:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)


def _held(
    capture: DjangoCaptureOnCommitCallbacks, request: Callable[[], Any]
) -> tuple[Any, list[Callable[[], Any]]]:
    """Make a request, failing if anything sends mail before commit."""
    with (
        mock.patch.object(
            EmailMessage, "send", side_effect=AssertionError("sent in the request")
        ),
        capture() as callbacks,
    ):
        response = request()
    return response, callbacks


def _commit(callbacks: list[Callable[[], Any]]) -> None:
    for callback in callbacks:
        callback()


def _assert_queued_for(email: str) -> None:
    assert mail.outbox == []
    [row] = QueuedEmail.objects.all()
    assert row.to == email


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_signup_queues_the_verification_email(
    base: str, django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks
) -> None:
    client, headers = _client(base)

    response, callbacks = _held(
        django_capture_on_commit_callbacks,
        lambda: post(
            client,
            f"{base}/auth/signup",
            {"email": EMAIL, "password": PASSWORD, **SIGNUP_DETAILS},
            **headers,
        ),
    )

    assert response.status_code == 401
    _assert_queued_for(EMAIL)
    assert len(callbacks) == 1

    _commit(callbacks)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [EMAIL]
    assert VERIFY_PREFIX in mail.outbox[0].body
    assert not QueuedEmail.objects.exists()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_password_reset_queues_the_key_for_the_stored_address(
    base: str, django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks
) -> None:
    _verified_account()
    client, headers = _client(base)

    response, callbacks = _held(
        django_capture_on_commit_callbacks,
        lambda: post(
            client,
            f"{base}/auth/password/request",
            {"email": " Queue-Sam@Example.org "},
            **headers,
        ),
    )

    assert response.status_code == 200
    _assert_queued_for(EMAIL)

    _commit(callbacks)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [EMAIL]
    assert RESET_PREFIX in mail.outbox[0].body
    assert not QueuedEmail.objects.exists()


def test_a_verification_resend_is_queued(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
) -> None:
    _verified_account()
    token = login(Client(), APP, EMAIL).json()["meta"]["session_token"]
    _, added = _held(
        django_capture_on_commit_callbacks,
        lambda: post(
            Client(), f"{APP}/account/email", {"email": NEW_EMAIL}, **app_headers(token)
        ),
    )
    _commit(added)
    assert len(mail.outbox) == 1
    # allauth limits verification mails per address; start the window again.
    cache.clear()

    response, callbacks = _held(
        django_capture_on_commit_callbacks,
        lambda: Client().put(
            f"{APP}/account/email",
            {"email": NEW_EMAIL},
            content_type="application/json",
            headers=app_headers(token),
        ),
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 1
    [row] = QueuedEmail.objects.all()
    assert row.to == NEW_EMAIL

    _commit(callbacks)

    assert len(mail.outbox) == 2
    assert mail.outbox[1].to == [NEW_EMAIL]
    assert VERIFY_PREFIX in mail.outbox[1].body
    assert not QueuedEmail.objects.exists()


def test_a_failed_enqueue_still_answers_the_request(
    django_capture_on_commit_callbacks: DjangoCaptureOnCommitCallbacks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With the broker down at commit, sign-up succeeds and the row waits."""
    with mock.patch(
        "apps.emails.tasks.send_queued_email.delay",
        side_effect=ConnectionRefusedError("broker unreachable"),
    ):
        response, callbacks = _held(
            django_capture_on_commit_callbacks, lambda: signup(Client(), APP, EMAIL)
        )
        _commit(callbacks)

    assert response.status_code == 401
    _assert_queued_for(EMAIL)
    for record in caplog.records:
        assert EMAIL not in record.getMessage()
