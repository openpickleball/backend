"""Organizer Member, Club Role, and Club Organizer Grant changes are audited (#158).

Each add, change, and removal through the admin records one Audit Event in
the same transaction, with the acting Account. A removal is recorded before
the row goes. A refused change records nothing. No event holds an email or an Organizer's name. Made-up data only.
"""

import json
from typing import TYPE_CHECKING, Any

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import (
    Club,
    ClubOrganizerGrant,
    ClubRole,
    ClubRoleKind,
    MemberRole,
    Organizer,
    OrganizerMember,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verify,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ACCESS_ACTIONS = [
    action
    for action in AuditAction
    if action.value.split(".")[0]
    in {"organizer_member", "club_role", "club_organizer_grant"}
]


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def admin_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _access_events() -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(action__in=ACCESS_ACTIONS).order_by(
            "created_at", "id"
        )
    )


def _members_formset(
    organizer: Organizer, rows: list[dict[str, str]], initial: int
) -> dict[str, str]:
    data = {
        "name": organizer.name,
        "members-TOTAL_FORMS": str(len(rows)),
        "members-INITIAL_FORMS": str(initial),
        "members-MIN_NUM_FORMS": "0",
        "members-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for key, value in row.items():
            data[f"members-{index}-{key}"] = value
    return data


def _roles_formset(
    club: Club, rows: list[dict[str, str]], initial: int
) -> dict[str, str]:
    data = {
        "name": club.name,
        "city": club.city,
        "region": club.region,
        "roles-TOTAL_FORMS": str(len(rows)),
        "roles-INITIAL_FORMS": str(initial),
        "roles-MIN_NUM_FORMS": "0",
        "roles-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(rows):
        for key, value in row.items():
            data[f"roles-{index}-{key}"] = value
    return data


def _post_organizer(client: Client, organizer: Organizer, data: dict[str, str]) -> Any:
    return client.post(
        reverse("admin:clubs_organizer_change", args=[organizer.pk]), data
    )


def _post_club(client: Client, club: Club, data: dict[str, str]) -> Any:
    return client.post(reverse("admin:clubs_club_change", args=[club.pk]), data)


def _holds_no_identifying_value(event: AuditEvent, *values: str) -> None:
    stored = json.dumps([event.before, event.after, event.reason])
    for value in values:
        assert value not in stored


# Organizer Members through the Organizer's inline.


def test_adding_a_member_in_the_inline_records_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [{"account": str(account.pk), "role": MemberRole.ADMIN, "id": ""}],
            initial=0,
        ),
    )

    assert response.status_code == 302
    member = OrganizerMember.objects.get(organizer=organizer)
    [event] = _access_events()
    assert event.action == AuditAction.ORGANIZER_MEMBER_ADD
    assert event.actor_user == superuser
    assert event.target == member
    assert event.before is None
    assert event.after == {
        "organizer": organizer.public_code,
        "account": account.pk,
        "role": MemberRole.ADMIN,
    }
    _holds_no_identifying_value(event, account.email, organizer.name)


def test_changing_a_member_role_in_the_inline_records_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    member = OrganizerMemberFactory.create(role=MemberRole.MATCH_MANAGER)
    organizer = member.organizer
    before = len(_access_events())

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [
                {
                    "id": str(member.pk),
                    "organizer": str(organizer.pk),
                    "account": str(member.account_id),
                    "role": MemberRole.ADMIN,
                }
            ],
            initial=1,
        ),
    )

    assert response.status_code == 302
    events = _access_events()[before:]
    [event] = events
    assert event.action == AuditAction.ORGANIZER_MEMBER_CHANGE
    assert event.actor_user == superuser
    assert event.before == {
        "organizer": organizer.public_code,
        "account": member.account_id,
        "role": MemberRole.MATCH_MANAGER,
    }
    assert event.after == {**event.before, "role": MemberRole.ADMIN}
    _holds_no_identifying_value(event, member.account.email, organizer.name)


def test_removing_a_member_in_the_inline_records_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    member = OrganizerMemberFactory.create(role=MemberRole.ADMIN)
    organizer = member.organizer
    member_pk = member.pk
    before = len(_access_events())

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [
                {
                    "id": str(member.pk),
                    "organizer": str(organizer.pk),
                    "account": str(member.account_id),
                    "role": MemberRole.ADMIN,
                    "DELETE": "on",
                }
            ],
            initial=1,
        ),
    )

    assert response.status_code == 302
    assert not OrganizerMember.objects.filter(pk=member_pk).exists()
    [event] = _access_events()[before:]
    assert event.action == AuditAction.ORGANIZER_MEMBER_REMOVE
    assert event.actor_user == superuser
    assert event.target_object_id == str(member_pk)
    assert event.before == {
        "organizer": organizer.public_code,
        "account": member.account_id,
        "role": MemberRole.ADMIN,
    }
    assert event.after is None


def test_an_unchanged_inline_records_nothing(admin_client: Client) -> None:
    member = OrganizerMemberFactory.create()
    organizer = member.organizer
    before = len(_access_events())

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [
                {
                    "id": str(member.pk),
                    "organizer": str(organizer.pk),
                    "account": str(member.account_id),
                    "role": member.role,
                }
            ],
            initial=1,
        ),
    )

    assert response.status_code == 302
    assert len(_access_events()) == before


def test_the_verifier_refused_as_member_in_the_inline_records_nothing(
    admin_client: Client,
) -> None:
    organizer = verify(OrganizerFactory.create())
    assert organizer.verified_by_id is not None
    before = AuditEvent.objects.count()

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [
                {
                    "account": str(organizer.verified_by_id),
                    "role": MemberRole.ADMIN,
                    "id": "",
                }
            ],
            initial=0,
        ),
    )

    assert response.status_code == 200
    assert not OrganizerMember.objects.filter(organizer=organizer).exists()
    assert AuditEvent.objects.count() == before


def test_an_invalid_inline_role_records_nothing(admin_client: Client) -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()

    response = _post_organizer(
        admin_client,
        organizer,
        _members_formset(
            organizer,
            [{"account": str(account.pk), "role": "owner", "id": ""}],
            initial=0,
        ),
    )

    assert response.status_code == 200
    assert _access_events() == []


# Club Roles through the Club's inline.


def test_club_role_add_and_remove_in_the_inline_each_record_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    club = ClubFactory.create()
    account = UserFactory.create()

    added = _post_club(
        admin_client,
        club,
        _roles_formset(
            club,
            [{"account": str(account.pk), "role": ClubRoleKind.ADMIN, "id": ""}],
            initial=0,
        ),
    )
    assert added.status_code == 302
    role = ClubRole.objects.get(club=club)
    removed = _post_club(
        admin_client,
        club,
        _roles_formset(
            club,
            [
                {
                    "id": str(role.pk),
                    "club": str(club.pk),
                    "account": str(account.pk),
                    "role": ClubRoleKind.ADMIN,
                    "DELETE": "on",
                }
            ],
            initial=1,
        ),
    )

    assert removed.status_code == 302
    values = {"club": club.public_code, "account": account.pk, "role": "admin"}
    add, remove = _access_events()
    assert (add.action, add.before, add.after) == (
        AuditAction.CLUB_ROLE_ADD,
        None,
        values,
    )
    assert (remove.action, remove.before, remove.after) == (
        AuditAction.CLUB_ROLE_REMOVE,
        values,
        None,
    )
    assert add.actor_user == superuser
    assert remove.actor_user == superuser
    for event in (add, remove):
        _holds_no_identifying_value(event, account.email)


# Club Organizer Grants through their own admin.


def test_grant_admin_add_change_and_delete_each_record_one_event(
    admin_client: Client, superuser: "_User"
) -> None:
    club = ClubFactory.create()
    other_club = ClubFactory.create()
    organizer = OrganizerFactory.create()

    added = admin_client.post(
        reverse("admin:clubs_cluborganizergrant_add"),
        {"club": str(club.pk), "organizer": str(organizer.pk)},
    )
    assert added.status_code == 302
    grant = ClubOrganizerGrant.objects.get()
    changed = admin_client.post(
        reverse("admin:clubs_cluborganizergrant_change", args=[grant.pk]),
        {"club": str(other_club.pk), "organizer": str(organizer.pk)},
    )
    assert changed.status_code == 302
    deleted = admin_client.post(
        reverse("admin:clubs_cluborganizergrant_delete", args=[grant.pk]),
        {"post": "yes"},
    )

    assert deleted.status_code == 302
    first = {"club": club.public_code, "organizer": organizer.public_code}
    second = {"club": other_club.public_code, "organizer": organizer.public_code}
    events = _access_events()
    assert [(e.action, e.before, e.after) for e in events] == [
        (AuditAction.CLUB_ORGANIZER_GRANT_ADD, None, first),
        (AuditAction.CLUB_ORGANIZER_GRANT_CHANGE, first, second),
        (AuditAction.CLUB_ORGANIZER_GRANT_REMOVE, second, None),
    ]
    assert {e.actor_user for e in events} == {superuser}
    for event in events:
        _holds_no_identifying_value(event, organizer.name)


def test_grant_delete_selected_records_one_event_per_grant(
    admin_client: Client, superuser: "_User"
) -> None:
    grants = ClubOrganizerGrantFactory.create_batch(2)
    before = len(_access_events())

    response = admin_client.post(
        reverse("admin:clubs_cluborganizergrant_changelist"),
        {
            "action": "delete_selected",
            "_selected_action": [str(g.pk) for g in grants],
            "post": "yes",
        },
    )

    assert response.status_code == 302
    events = _access_events()[before:]
    assert sorted(e.target_object_id for e in events) == sorted(
        str(g.pk) for g in grants
    )
    assert {e.action for e in events} == {AuditAction.CLUB_ORGANIZER_GRANT_REMOVE}
    assert {e.actor_user for e in events} == {superuser}


def test_a_repeated_grant_is_refused_and_records_nothing() -> None:
    grant = ClubOrganizerGrantFactory.create()
    before = AuditEvent.objects.count()

    with pytest.raises(IntegrityError), transaction.atomic():
        ClubOrganizerGrant.objects.create(club=grant.club, organizer=grant.organizer)

    assert AuditEvent.objects.count() == before


# A refused delete.


def test_a_refused_verified_organizer_delete_records_nothing(
    admin_client: Client,
) -> None:
    organizer = verify(OrganizerFactory.create())
    OrganizerMemberFactory.create(organizer=organizer)
    before = AuditEvent.objects.count()

    admin_client.post(
        reverse("admin:clubs_organizer_delete", args=[organizer.pk]),
        {"post": "yes"},
    )

    assert OrganizerMember.objects.filter(organizer=organizer).exists()
    assert AuditEvent.objects.count() == before


# Refusals through the ORM record nothing either.


def test_the_verifier_refused_by_save_records_nothing() -> None:
    organizer = verify(OrganizerFactory.create())
    verifier = organizer.verified_by
    assert verifier is not None
    before = AuditEvent.objects.count()

    with pytest.raises(ValidationError):
        OrganizerMember.objects.create(
            organizer=organizer, account=verifier, role=MemberRole.ADMIN
        )

    assert AuditEvent.objects.count() == before


def test_a_failed_save_rolls_back_its_event() -> None:
    member = OrganizerMemberFactory.create()
    before = AuditEvent.objects.count()
    duplicate = OrganizerMember(
        organizer=member.organizer, account=member.account, role=MemberRole.ADMIN
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()

    assert AuditEvent.objects.count() == before
