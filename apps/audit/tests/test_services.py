"""record_event and snapshot store audit events with redacted diffs."""

import datetime
import uuid
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import FieldDoesNotExist
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.audit.services import record_event, snapshot

pytestmark = pytest.mark.django_db

REDACTED = "[redacted]"


def test_record_event_stores_every_field() -> None:
    actor = UserFactory.create()
    target = UserFactory.create()
    started = timezone.now()

    event = record_event(
        action=AuditAction.RATING_EDIT,
        target=target,
        actor=actor,
        before={"is_active": True},
        after={"is_active": False},
        reason="Made-up correction for a test.",
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.actor_user_id == actor.pk
    assert stored.action == AuditAction.RATING_EDIT
    assert stored.target_content_type == ContentType.objects.get_for_model(target)
    assert stored.target_object_id == str(target.pk)
    assert stored.target == target
    assert stored.before == {"is_active": True}
    assert stored.after == {"is_active": False}
    assert stored.reason == "Made-up correction for a test."
    assert started <= stored.created_at <= timezone.now()
    assert AuditEvent.objects.count() == 1


@pytest.mark.parametrize("action", list(AuditAction))
def test_record_event_accepts_each_action(action: AuditAction) -> None:
    target = UserFactory.create()

    event = record_event(action=action, target=target)

    assert AuditEvent.objects.get(pk=event.pk).action == action.value


def test_record_event_accepts_an_action_value_string() -> None:
    target = UserFactory.create()

    event = record_event(action="mfa.change", target=target)  # type: ignore[arg-type]

    assert AuditEvent.objects.get(pk=event.pk).action == AuditAction.MFA_CHANGE


def test_create_diff_has_empty_before_and_set_after() -> None:
    target = UserFactory.create(is_staff=False)

    event = record_event(
        action=AuditAction.ADMIN_ADD,
        target=target,
        after=snapshot(target, ["email", "is_staff"]),
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before is None
    assert stored.after == {"email": target.email, "is_staff": False}


def test_change_diff_has_before_and_after() -> None:
    target = UserFactory.create(is_staff=False)
    before = snapshot(target, ["is_staff"])
    target.is_staff = True
    target.save()

    event = record_event(
        action=AuditAction.ADMIN_CHANGE,
        target=target,
        before=before,
        after=snapshot(target, ["is_staff"]),
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before == {"is_staff": False}
    assert stored.after == {"is_staff": True}


def test_delete_diff_has_set_before_and_empty_after() -> None:
    target = UserFactory.create()
    before = snapshot(target, ["email", "is_active"])

    event = record_event(action=AuditAction.ADMIN_DELETE, target=target, before=before)

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before == {"email": target.email, "is_active": True}
    assert stored.after is None


def test_snapshot_returns_only_the_named_fields() -> None:
    target = UserFactory.create()

    assert set(snapshot(target, ["email"])) == {"email"}


def test_snapshot_rejects_an_unknown_field() -> None:
    target = UserFactory.create()

    with pytest.raises(FieldDoesNotExist):
        snapshot(target, ["no_such_field"])


def test_snapshot_gives_a_foreign_key_as_its_primary_key() -> None:
    actor = UserFactory.create()
    event = record_event(action=AuditAction.SESSION_END, target=actor, actor=actor)

    assert snapshot(event, ["actor_user"]) == {"actor_user": actor.pk}


def test_record_event_redacts_sensitive_keys_at_any_depth() -> None:
    target = UserFactory.create()
    before: dict[str, Any] = {
        "password": "made-up-password-1",
        "Profile": {
            "API_KEY": "made-up-key-1",
            "settings": [{"otp_secret": "made-up-otp-1"}, {"theme": "dark"}],
        },
        "email": "kept@example.com",
    }
    after: dict[str, Any] = {
        "passwd": "made-up-password-2",
        "client_secret": "made-up-secret-2",
        "access_token": "made-up-token-2",
        "recovery_codes": ["made-up-code-2"],
        "private_key": "made-up-private-2",
        "session_key": "made-up-session-2",
        "nested": ({"refresh_TOKEN": "made-up-token-3"},),
        "rating": 4,
    }

    event = record_event(
        action=AuditAction.PASSWORD_CHANGE, target=target, before=before, after=after
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before == {
        "password": REDACTED,
        "Profile": {
            "API_KEY": REDACTED,
            "settings": [{"otp_secret": REDACTED}, {"theme": "dark"}],
        },
        "email": "kept@example.com",
    }
    assert stored.after == {
        "passwd": REDACTED,
        "client_secret": REDACTED,
        "access_token": REDACTED,
        "recovery_codes": REDACTED,
        "private_key": REDACTED,
        "session_key": REDACTED,
        "nested": [{"refresh_TOKEN": REDACTED}],
        "rating": 4,
    }


def test_redacted_values_never_reach_the_row() -> None:
    target = UserFactory.create()

    event = record_event(
        action=AuditAction.MFA_CHANGE,
        target=target,
        after={"device": {"totp_secret": "made-up-raw-secret-value"}},
    )

    raw = AuditEvent.objects.filter(pk=event.pk).values_list("after", flat=True).get()
    assert "made-up-raw-secret-value" not in str(raw)


def test_record_event_does_not_change_the_callers_dict() -> None:
    target = UserFactory.create()
    after = {"nested": {"token": "made-up-token"}, "items": [{"secret": "x"}]}

    record_event(action=AuditAction.MFA_CHANGE, target=target, after=after)

    assert after == {"nested": {"token": "made-up-token"}, "items": [{"secret": "x"}]}


def test_record_event_rejects_an_unsaved_target() -> None:
    unsaved = get_user_model()(email="unsaved@example.com")

    with pytest.raises(ValueError, match="target"):
        record_event(action=AuditAction.ADMIN_ADD, target=unsaved)

    assert not AuditEvent.objects.exists()


def test_record_event_rejects_an_unsaved_actor() -> None:
    target = UserFactory.create()
    unsaved = get_user_model()(email="unsaved@example.com")

    with pytest.raises(ValueError, match="actor"):
        record_event(action=AuditAction.ADMIN_ADD, target=target, actor=unsaved)

    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("action", ["admin.nope", "", "ADMIN_ADD", None, 3])
def test_record_event_rejects_an_unknown_action(action: Any) -> None:
    target = UserFactory.create()

    with pytest.raises(ValueError, match="action") as excinfo:
        record_event(action=action, target=target)

    assert "admin.nope" not in str(excinfo.value)
    assert not AuditEvent.objects.exists()


@pytest.mark.parametrize("actor", [None, AnonymousUser()])
def test_anonymous_or_missing_actor_stores_no_actor(actor: AnonymousUser | None) -> None:
    target = UserFactory.create()

    event = record_event(action=AuditAction.SESSION_END, target=target, actor=actor)

    assert AuditEvent.objects.get(pk=event.pk).actor_user_id is None


def test_record_event_stores_values_json_cannot_hold_natively() -> None:
    target = UserFactory.create()
    when = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")

    event = record_event(
        action=AuditAction.RATING_EDIT,
        target=target,
        before={"rating": Decimal("3.75"), "at": when, "id": ident},
        after={"note": "Joueur fictif, cote révisée"},
    )

    stored = AuditEvent.objects.get(pk=event.pk)
    assert stored.before == {
        "rating": "3.75",
        "at": "2026-01-02T03:04:05Z",
        "id": str(ident),
    }
    assert stored.after == {"note": "Joueur fictif, cote révisée"}


def test_str_names_the_action_and_target_without_values() -> None:
    target = UserFactory.create()
    event = record_event(
        action=AuditAction.RATING_EDIT,
        target=target,
        before={"rating": "made-up-value"},
    )

    text = str(event)
    assert "Manual rating edit" in text
    assert str(target.pk) in text
    assert "made-up-value" not in text
