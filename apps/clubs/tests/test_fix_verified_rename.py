"""A verified Organizer cannot be renamed through self-service (#229).

Verification is a moderator's review of who the Organizer is, so a
verified Organizer taking another name would keep its trusted-submitter
authority under a name nobody reviewed (hard rule 8). The rename is
refused with 409 and fixed text, and nothing changes. Made-up data only.
"""

import json

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, Organizer
from apps.clubs.self_service import Conflict, rename_organizer
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer

pytestmark = pytest.mark.django_db

MESSAGE = (
    "A verified organizer cannot be renamed here. Ask a moderator to unverify it first."
)


def _admin_of_verified() -> tuple[User, Organizer]:
    organizer = verified_organizer(name="Made-up Reviewed Name")
    account: User = UserFactory.create()
    EmailAddress.objects.create(user=account, email=account.email, verified=True)
    enroll_totp(account)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account, organizer


def test_the_service_refuses_to_rename_a_verified_organizer() -> None:
    account, organizer = _admin_of_verified()

    with pytest.raises(Conflict, match="cannot be renamed"):
        rename_organizer(organizer.pk, actor=account, name="Made-up Other Club")

    organizer.refresh_from_db()
    assert organizer.name == "Made-up Reviewed Name"


def test_the_api_answers_409_with_fixed_text() -> None:
    account, organizer = _admin_of_verified()
    client = Client()
    client.force_login(account)

    response = client.patch(
        f"/api/v1/organizer/organizers/{organizer.public_code}",
        data=json.dumps({"name": "Made-up Other Club"}),
        content_type="application/json",
    )

    assert response.status_code == 409
    assert response.json()["message"] == MESSAGE
    assert "Made-up Other Club" not in response.content.decode()
    assert not AuditEvent.objects.filter(action=AuditAction.ORGANIZER_CHANGE).exists()
