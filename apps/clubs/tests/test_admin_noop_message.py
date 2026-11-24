"""The admin actions count only Organizers whose status changed."""

from typing import TYPE_CHECKING

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import Organizer
from apps.clubs.services import verify_organizer

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REASON = "Made-up verification reason."


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def superuser_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _run(client: Client, action: str, organizers: list[Organizer]) -> list[str]:
    response = client.post(
        reverse("admin:clubs_organizer_changelist"),
        {
            "action": action,
            "_selected_action": [str(o.pk) for o in organizers],
            "reason": REASON,
        },
        follow=True,
    )
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_verifying_a_verified_organizer_reports_no_change(
    superuser_client: Client, superuser: "_User"
) -> None:
    organizer = Organizer.objects.create(name="Made-up Events")
    verify_organizer(organizer, actor=superuser)

    messages = _run(superuser_client, "verify_selected", [organizer])

    assert not any("verified." in message for message in messages)


def test_unverifying_an_unverified_organizer_reports_no_change(
    superuser_client: Client,
) -> None:
    organizer = Organizer.objects.create(name="Made-up Events")

    messages = _run(superuser_client, "unverify_selected", [organizer])

    assert not any("unverified." in message for message in messages)


def test_mixed_selection_counts_only_changes(
    superuser_client: Client, superuser: "_User"
) -> None:
    already = Organizer.objects.create(name="Made-up Events")
    verify_organizer(already, actor=superuser)
    new = Organizer.objects.create(name="Made-up Other Events")

    messages = _run(superuser_client, "verify_selected", [already, new])

    assert "1 organizer(s) verified." in messages
