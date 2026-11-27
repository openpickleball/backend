"""The admin for Organizer Verification Requests (#229, ADR 0054).

Moderators see requests read-only, never those on an Organizer they are
a member of, and close one without verifying through an action with a
typed reason. Messages hold no name or statement. Made-up data only.
"""

import pytest
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, OrganizerVerificationRequest
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    OrganizerVerificationRequestFactory,
)
from apps.matches.permissions import MODERATOR_GROUP

pytestmark = pytest.mark.django_db

CHANGELIST = "admin:clubs_organizerverificationrequest_changelist"
STATEMENT = "Made-up weekly ladder statement."
NAME = "Made-up Organizer Person"


def _moderator() -> User:
    account: User = UserFactory.create(is_staff=True)
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _request() -> OrganizerVerificationRequest:
    return OrganizerVerificationRequestFactory.create(
        organizer=OrganizerFactory.create(name=NAME), statement=STATEMENT
    )


def _close(
    client: Client, request: OrganizerVerificationRequest, reason: str
) -> list[str]:
    response = client.post(
        reverse(CHANGELIST),
        {
            "action": "close_selected",
            "_selected_action": [str(request.pk)],
            "reason": reason,
        },
        follow=True,
    )
    assert response.status_code == 200
    return [str(m) for m in get_messages(response.wsgi_request)]


def test_a_moderator_sees_the_request_and_its_statement() -> None:
    request = _request()
    client = _client(_moderator())

    listing = client.get(reverse(CHANGELIST))
    page = client.get(
        reverse("admin:clubs_organizerverificationrequest_change", args=[request.pk])
    )

    assert listing.status_code == 200
    assert request.organizer.public_code in listing.content.decode()
    assert page.status_code == 200
    assert STATEMENT in page.content.decode()


def test_a_member_moderator_does_not_see_their_organizers_request() -> None:
    request = _request()
    moderator = _moderator()
    OrganizerMemberFactory.create(
        organizer=request.organizer, account=moderator, role=MemberRole.ADMIN
    )
    client = _client(moderator)

    listing = client.get(reverse(CHANGELIST))
    page = client.get(
        reverse("admin:clubs_organizerverificationrequest_change", args=[request.pk])
    )

    assert request.organizer.public_code not in listing.content.decode()
    assert page.status_code in (302, 404)
    assert STATEMENT not in page.content.decode()


def test_staff_without_the_view_permission_cannot_open_it() -> None:
    _request()
    client = _client(UserFactory.create(is_staff=True))

    assert client.get(reverse(CHANGELIST)).status_code == 403


def test_the_admin_cannot_add_change_or_delete() -> None:
    request = _request()
    superuser: User = UserFactory.create(is_staff=True, is_superuser=True)
    client = _client(superuser)

    assert (
        client.get(reverse("admin:clubs_organizerverificationrequest_add")).status_code
        == 403
    )
    assert (
        client.get(
            reverse(
                "admin:clubs_organizerverificationrequest_delete", args=[request.pk]
            )
        ).status_code
        == 403
    )


def test_a_moderator_closes_with_a_reason_and_nothing_is_verified() -> None:
    request = _request()

    messages = _close(_client(_moderator()), request, "Made-up close reason.")

    request.refresh_from_db()
    request.organizer.refresh_from_db()
    assert request.closed_at is not None
    assert request.organizer.verified_at is None
    assert messages == ["1 request(s) closed."]
    [event] = AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_VERIFICATION_REQUEST_CLOSE
    )
    assert event.reason == "Made-up close reason."
    for message in messages:
        assert NAME not in message
        assert STATEMENT not in message


def test_a_blank_reason_closes_nothing() -> None:
    request = _request()

    messages = _close(_client(_moderator()), request, "  ")

    request.refresh_from_db()
    assert request.closed_at is None
    assert messages == ["Give a reason to close verification requests."]


def test_viewing_staff_without_verify_cannot_run_the_close() -> None:
    request = _request()
    viewer: User = UserFactory.create(is_staff=True)
    viewer.user_permissions.add(
        Permission.objects.get(codename="view_organizerverificationrequest")
    )

    _close(_client(viewer), request, "Made-up reason.")

    request.refresh_from_db()
    assert request.closed_at is None
