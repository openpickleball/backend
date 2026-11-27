"""The signed-in Account's own Organizer memberships (#301, ADR 0050).

``GET /api/v1/organizer/organizers`` lists each Organizer the Account is
a member of, verified or not, with its OPEN Code, name, verified state,
and the Account's own role. It never lists another Account's memberships
or the other members of the Account's Organizers. Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import MemberRole, Organizer
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event

pytestmark = pytest.mark.django_db

ORGANIZERS = "/api/v1/organizer/organizers"
EVENTS = "/api/v1/organizer/events"


def _member(
    organizer: Organizer,
    role: MemberRole = MemberRole.ADMIN,
    account: User | None = None,
) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(organizer=organizer, account=member, role=role)
    return member


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _list(account: User) -> Any:
    response = _client(account).get(ORGANIZERS)
    assert response.status_code == 200
    return response.json()


def test_a_member_sees_verified_and_unverified_organizers_with_role() -> None:
    verified = verified_organizer(name="Made-up Verified Ladder")
    unverified = OrganizerFactory.create(name="Made-up Unverified League")
    account = _member(verified, MemberRole.MATCH_MANAGER)
    _member(unverified, MemberRole.ADMIN, account=account)

    expected = sorted(
        [
            {
                "organizer": verified.public_code,
                "name": "Made-up Verified Ladder",
                "verified": True,
                "verification_requested": False,
                "role": "match_manager",
            },
            {
                "organizer": unverified.public_code,
                "name": "Made-up Unverified League",
                "verified": False,
                "verification_requested": False,
                "role": "admin",
            },
        ],
        key=lambda item: str(item["organizer"]),
    )
    assert _list(account) == expected


def test_an_account_with_no_memberships_gets_an_empty_list() -> None:
    _member(verified_organizer())
    assert _list(UserFactory.create()) == []


def test_no_other_account_or_member_is_listed() -> None:
    shared = verified_organizer()
    other_only = OrganizerFactory.create()
    account = _member(shared, MemberRole.MATCH_MANAGER)
    other = _member(shared, MemberRole.ADMIN)
    _member(other_only, MemberRole.ADMIN, account=other)

    items = _list(account)

    assert [item["organizer"] for item in items] == [shared.public_code]
    # The Account's own role, never the other member's.
    assert items[0]["role"] == "match_manager"
    # Only these fields: no member list, Account, email, or row id.
    assert set(items[0]) == {
        "organizer",
        "name",
        "verified",
        "verification_requested",
        "role",
    }
    assert other.email not in str(items)


def test_unverify_shows_on_the_next_read() -> None:
    organizer = verified_organizer()
    account = _member(organizer)
    staff: User = UserFactory.create()
    staff.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(organizer, actor=staff)
    assert _list(account)[0]["verified"] is False


def test_the_code_is_the_one_an_event_create_takes() -> None:
    organizer = verified_organizer()
    account = _member(organizer, MemberRole.ADMIN)
    client = _client(account)

    code = client.get(ORGANIZERS).json()[0]["organizer"]
    response = client.post(
        EVENTS,
        {
            "organizer": code,
            "name": "Made-up Spring Ladder",
            "kind": "league",
            "start_date": "2026-03-01",
            "end_date": "2026-05-31",
        },
        content_type="application/json",
    )

    assert response.status_code == 201
    assert Event.objects.get(public_code=response.json()["code"]).organizer == (
        organizer
    )


def test_sign_in_is_required() -> None:
    response = Client().get(ORGANIZERS)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_memberships_come_back_ordered_by_code() -> None:
    organizers = sorted(
        (OrganizerFactory.create() for _ in range(4)),
        key=lambda organizer: organizer.public_code,
        reverse=True,
    )
    account: User = UserFactory.create()
    # Joined in reverse code order, so insertion order is not code order.
    for organizer in organizers:
        _member(organizer, account=account)

    codes = [item["organizer"] for item in _list(account)]
    assert codes == sorted(organizer.public_code for organizer in organizers)
