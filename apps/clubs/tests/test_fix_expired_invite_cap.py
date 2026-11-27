"""An expired earlier invite does not skip the open-invite cap (#229).

Re-inviting an address whose earlier invite expired (and was not swept
yet) adds an open invite, so it must count against
``ORGANIZER_OPEN_INVITES_LIMIT`` like a new address. Made-up data only.
"""

from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import MemberRole, OrganizerInvite
from apps.clubs.self_service import Conflict, invite_member
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerInviteFactory,
    OrganizerMemberFactory,
)

pytestmark = pytest.mark.django_db


def test_reinviting_an_expired_address_at_the_cap_is_refused(settings: Any) -> None:
    settings.ORGANIZER_OPEN_INVITES_LIMIT = 1
    organizer = OrganizerFactory.create()
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    OrganizerMemberFactory.create(
        organizer=organizer, account=admin, role=MemberRole.ADMIN
    )
    OrganizerInviteFactory.create(organizer=organizer, email="open@example.com")
    OrganizerInviteFactory.create(
        organizer=organizer,
        email="expired@example.com",
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    with pytest.raises(Conflict):
        invite_member(
            organizer.pk, actor=admin, email="expired@example.com", role="admin"
        )

    open_count = OrganizerInvite.objects.filter(
        organizer=organizer, expires_at__gt=timezone.now()
    ).count()
    assert open_count == 1
