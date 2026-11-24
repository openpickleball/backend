"""The action counts what its own call changed, not a stale before-read."""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs import admin as clubs_admin
from apps.clubs import services
from apps.clubs.models import Organizer

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REASON = "Made-up verification reason."


def _run(client: Client, action: str, organizer: Organizer) -> list[str]:
    response = client.post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": action,
            "_selected_action": [str(organizer.pk)],
            "reason": REASON,
        },
        follow=True,
    )
    assert response.status_code == 200
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_verified_by_someone_else_first_is_not_counted(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    superuser: _User = UserFactory.create(is_staff=True, is_superuser=True)
    other: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(superuser)
    organizer = Organizer.objects.create(name="Made-up Events")

    def other_verifies_first(target: Organizer, **kwargs: Any) -> Organizer:
        # Another verifier gets there after the action loaded the row.
        services.verify_organizer(Organizer.objects.get(pk=target.pk), actor=other)
        return services.verify_organizer(target, **kwargs)

    monkeypatch.setattr(clubs_admin, "verify_organizer", other_verifies_first)

    messages = _run(client, "verify_selected", organizer)

    assert Organizer.objects.get(pk=organizer.pk).verified_by_id == other.pk
    assert AuditEvent.objects.count() == 1
    assert not any("verified." in message for message in messages)


def test_unverified_by_someone_else_first_is_not_counted(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    superuser: _User = UserFactory.create(is_staff=True, is_superuser=True)
    other: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(superuser)
    organizer = Organizer.objects.create(name="Made-up Events")
    services.verify_organizer(organizer, actor=other)

    def other_unverifies_first(target: Organizer, **kwargs: Any) -> Organizer:
        services.unverify_organizer(Organizer.objects.get(pk=target.pk), actor=other)
        return services.unverify_organizer(target, **kwargs)

    monkeypatch.setattr(clubs_admin, "unverify_organizer", other_unverifies_first)

    messages = _run(client, "unverify_selected", organizer)

    assert Organizer.objects.get(pk=organizer.pk).is_verified is False
    assert AuditEvent.objects.count() == 2
    assert not any("unverified." in message for message in messages)


def test_service_returns_are_unchanged() -> None:
    verifier: _User = UserFactory.create(is_staff=True, is_superuser=True)
    organizer = Organizer.objects.create(name="Made-up Events")

    assert services.verify_organizer(organizer, actor=verifier) is organizer
    assert services.unverify_organizer(organizer, actor=verifier) is organizer
