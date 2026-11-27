"""An admin who demotes themselves gets the changed membership (#229).

Another active admin remains, so the demotion is allowed and commits;
the answer must be 200 with the new role, not a 404 from a listing the
now match manager may no longer read. Made-up data only.
"""

import json

import pytest
from allauth.account.models import EmailAddress
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs.models import MemberRole, OrganizerMember
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _admin(organizer: object) -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    PlayerFactory.create(account=account)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def test_an_admin_demoting_themselves_gets_200_with_the_new_role() -> None:
    organizer = OrganizerFactory.create()
    first = _admin(organizer)
    _admin(organizer)
    client = Client()
    client.force_login(first)
    code = first.player.public_code

    response = client.patch(
        f"/api/v1/organizer/organizers/{organizer.public_code}/members/{code}",
        data=json.dumps({"role": "match_manager"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert response.json() == {
        "player": code,
        "display_name": first.player.display_name,
        "role": "match_manager",
        "you": True,
    }
    assert OrganizerMember.objects.get(account=first).role == MemberRole.MATCH_MANAGER
