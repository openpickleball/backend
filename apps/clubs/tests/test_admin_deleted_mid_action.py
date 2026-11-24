"""An Organizer deleted while an admin action runs is reported, not a 500."""

from typing import TYPE_CHECKING, Any

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs import admin as clubs_admin
from apps.clubs import services
from apps.clubs.models import Organizer

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REASON = "Made-up verification reason."


def test_organizer_deleted_mid_action_is_warned_by_code(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    superuser: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(superuser)
    gone = Organizer.objects.create(name="Made-up Person Name")
    kept = Organizer.objects.create(name="Made-up Other Events")

    def delete_then_verify(organizer: Organizer, **kwargs: Any) -> Organizer:
        # Another session deletes the row after the action loaded it.
        if organizer.pk == gone.pk:
            Organizer.objects.filter(pk=gone.pk).delete()
        return services.verify_organizer(organizer, **kwargs)

    monkeypatch.setattr(clubs_admin, "verify_organizer", delete_then_verify)

    response = client.post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": "verify_selected",
            "_selected_action": [str(gone.pk), str(kept.pk)],
            "reason": REASON,
        },
        follow=True,
    )

    assert response.status_code == 200
    messages = [str(message) for message in get_messages(response.wsgi_request)]
    text = " ".join(messages)
    assert f"{gone.public_code} was not changed: it no longer exists." in messages
    assert "Made-up Person Name" not in text
    assert Organizer.objects.get(pk=kept.pk).is_verified is True
    assert "1 organizer(s) verified." in messages
