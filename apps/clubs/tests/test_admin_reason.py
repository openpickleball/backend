"""The organizer verify and unverify actions need a typed reason (#40).

The reason goes into the Audit Event. A blank one changes nothing.
Made-up data only.
"""

from typing import TYPE_CHECKING

import pytest
from django.contrib.messages import get_messages
from django.contrib.messages.storage.base import Message
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import Organizer
from apps.clubs.tests.factories import OrganizerFactory, verified_organizer

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:clubs_organizer_changelist"
BLANK_WARNING = "Give a reason to verify or unverify organizers."


@pytest.fixture
def superuser_client(client: Client) -> Client:
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    client.force_login(account)
    return client


def _run(
    client: Client, action: str, organizer: Organizer, reason: str | None
) -> list[Message]:
    data = {"action": action, "_selected_action": [str(organizer.pk)]}
    if reason is not None:
        data["reason"] = reason
    response = client.post(reverse(CHANGELIST), data, follow=True)
    assert response.status_code == 200
    return list(get_messages(response.wsgi_request))


def test_changelist_has_a_reason_box(superuser_client: Client) -> None:
    OrganizerFactory.create()

    page = superuser_client.get(reverse(CHANGELIST)).content.decode()

    assert 'name="reason"' in page


@pytest.mark.parametrize("reason", [None, "", "   \t "])
def test_verify_without_a_reason_changes_nothing(
    superuser_client: Client, reason: str | None
) -> None:
    organizer = OrganizerFactory.create()

    messages = _run(superuser_client, "verify_selected", organizer, reason)

    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert not AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).exists()
    assert [str(m) for m in messages] == [BLANK_WARNING]
    assert messages[0].level_tag == "warning"


@pytest.mark.parametrize("reason", [None, "", "  "])
def test_unverify_without_a_reason_changes_nothing(
    superuser_client: Client, reason: str | None
) -> None:
    organizer = verified_organizer()

    messages = _run(superuser_client, "unverify_selected", organizer, reason)

    organizer.refresh_from_db()
    assert organizer.is_verified is True
    assert not AuditEvent.objects.filter(action=AuditAction.ORGANIZER_UNVERIFY).exists()
    assert [str(m) for m in messages] == [BLANK_WARNING]


def test_verify_records_the_typed_reason_stripped(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()

    _run(superuser_client, "verify_selected", organizer, "  Made-up checked docs. ")

    event = AuditEvent.objects.get(action=AuditAction.ORGANIZER_VERIFY)
    assert event.reason == "Made-up checked docs."


def test_unverify_records_the_typed_reason(superuser_client: Client) -> None:
    organizer = verified_organizer()

    _run(superuser_client, "unverify_selected", organizer, "Made-up lapsed.")

    event = AuditEvent.objects.get(action=AuditAction.ORGANIZER_UNVERIFY)
    assert event.reason == "Made-up lapsed."


def test_a_reason_over_the_limit_changes_nothing(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()

    _run(superuser_client, "verify_selected", organizer, "x" * 1001)

    organizer.refresh_from_db()
    assert organizer.is_verified is False
