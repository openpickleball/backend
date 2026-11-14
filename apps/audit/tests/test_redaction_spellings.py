"""Redaction catches sensitive keys however they are spelled.

Review round 1: camelCase and dashed keys (apiKey, api-key, privateKey,
sessionKey) slipped past the underscore-only pattern. A key is now
lowercased and stripped of every non-alphanumeric character before it is
matched.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event

pytestmark = pytest.mark.django_db

REDACTED = "[redacted]"


@pytest.mark.parametrize(
    "key",
    [
        "apiKey",
        "api-key",
        "API.Key",
        "privateKey",
        "private-key",
        "sessionKey",
        "session-key",
        "sessionData",
        "session_data",
        "otp_code",
        "OTP",
    ],
)
def test_key_is_redacted(key: str) -> None:
    raw = f"made-up-value-for-{key}"

    event = record_event(
        action=AuditAction.MFA_CHANGE,
        target=UserFactory.create(),
        after={"nested": {key: raw}},
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.after == {"nested": {key: REDACTED}}


@pytest.mark.parametrize("key", ["seed", "seed_rating", "session", "api", "rating"])
def test_innocent_key_is_kept(key: str) -> None:
    event = record_event(
        action=AuditAction.RATING_EDIT,
        target=UserFactory.create(),
        after={key: "made-up-value"},
    )

    assert AuditEvent.objects.get(pk=event.pk).after == {key: "made-up-value"}
