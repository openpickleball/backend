"""The admin shows Clubs and Organizers, and verifies only through actions."""

from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.models import Permission
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import (
    Club,
    ClubOrganizerGrant,
    MemberRole,
    Organizer,
    OrganizerMember,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

REASON = "Made-up verification reason."

ORGANIZER_CHANGELIST = "admin:clubs_organizer_changelist"
CLUB_CHANGELIST = "admin:clubs_club_changelist"


def _staff(*codenames: str) -> "_User":
    account: _User = UserFactory.create(is_staff=True)
    account.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="clubs", codename__in=codenames
        )
    )
    return account


@pytest.fixture
def superuser() -> "_User":
    account: _User = UserFactory.create(is_staff=True, is_superuser=True)
    return account


@pytest.fixture
def superuser_client(client: Client, superuser: "_User") -> Client:
    client.force_login(superuser)
    return client


def _run_action(client: Client, action: str, organizers: list[Organizer]) -> list[str]:
    response = client.post(
        reverse(ORGANIZER_CHANGELIST),
        {
            "action": action,
            "_selected_action": [str(o.pk) for o in organizers],
            "reason": REASON,
        },
        follow=True,
    )
    assert response.status_code == 200
    return [str(message) for message in get_messages(response.wsgi_request)]


def _organizer_change_data(organizer: Organizer, **extra: str) -> dict[str, str]:
    data = {
        "name": organizer.name,
        "members-TOTAL_FORMS": "0",
        "members-INITIAL_FORMS": "0",
        "members-MIN_NUM_FORMS": "0",
        "members-MAX_NUM_FORMS": "1000",
    }
    data.update(extra)
    return data


def test_changelists_load(superuser_client: Client) -> None:
    OrganizerFactory.create()
    ClubFactory.create()

    for name in (ORGANIZER_CHANGELIST, CLUB_CHANGELIST):
        assert superuser_client.get(reverse(name)).status_code == 200


def test_change_forms_load_with_inlines(superuser_client: Client) -> None:
    member = OrganizerMemberFactory.create()
    role = ClubRoleFactory.create()

    organizer_page = superuser_client.get(
        reverse("admin:clubs_organizer_change", args=[member.organizer.pk])
    )
    club_page = superuser_client.get(
        reverse("admin:clubs_club_change", args=[role.club.pk])
    )

    assert organizer_page.status_code == 200
    assert "members-TOTAL_FORMS" in organizer_page.content.decode()
    assert club_page.status_code == 200
    assert "roles-TOTAL_FORMS" in club_page.content.decode()


def test_club_role_inline_offers_only_admin(superuser_client: Client) -> None:
    role = ClubRoleFactory.create()

    page = superuser_client.get(
        reverse("admin:clubs_club_change", args=[role.club.pk])
    ).content.decode()

    assert 'value="admin"' in page
    assert "match_manager" not in page


def test_club_organizer_grant_admin_pages_load(superuser_client: Client) -> None:
    grant = ClubOrganizerGrantFactory.create()

    changelist = superuser_client.get(
        reverse("admin:clubs_cluborganizergrant_changelist")
    )
    add = superuser_client.get(reverse("admin:clubs_cluborganizergrant_add"))
    change = superuser_client.get(
        reverse("admin:clubs_cluborganizergrant_change", args=[grant.pk])
    )

    assert changelist.status_code == 200
    assert str(grant) in changelist.content.decode()
    assert add.status_code == 200
    assert change.status_code == 200


def test_club_organizer_grant_admin_adds_a_grant(superuser_client: Client) -> None:
    club = ClubFactory.create()
    organizer = OrganizerFactory.create()

    response = superuser_client.post(
        reverse("admin:clubs_cluborganizergrant_add"),
        {"club": str(club.pk), "organizer": str(organizer.pk)},
    )

    assert response.status_code == 302
    assert ClubOrganizerGrant.objects.filter(club=club, organizer=organizer).exists()


def test_change_form_cannot_set_verification(
    superuser_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    url = reverse("admin:clubs_organizer_change", args=[organizer.pk])

    response = superuser_client.post(
        url,
        _organizer_change_data(
            organizer,
            name="Made-up Renamed Events",
            verified_at_0="2026-01-01",
            verified_at_1="00:00:00",
            verified_at="2026-01-01 00:00:00",
            verified_by=str(superuser.pk),
        ),
    )

    assert response.status_code == 302
    organizer.refresh_from_db()
    assert organizer.name == "Made-up Renamed Events"
    assert organizer.verified_at is None
    assert organizer.verified_by is None
    assert not AuditEvent.objects.filter(
        action__in=[AuditAction.ORGANIZER_VERIFY, AuditAction.ORGANIZER_UNVERIFY]
    ).exists()


def test_change_form_cannot_clear_verification(
    superuser_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    verified_at = timezone.now()
    Organizer.objects.filter(pk=organizer.pk).update(
        verified_at=verified_at, verified_by=superuser
    )
    url = reverse("admin:clubs_organizer_change", args=[organizer.pk])

    response = superuser_client.post(
        url, _organizer_change_data(organizer, verified_at="", verified_by="")
    )

    assert response.status_code == 302
    organizer.refresh_from_db()
    assert organizer.verified_at == verified_at
    assert organizer.verified_by == superuser


def test_verification_fields_are_read_only(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()

    page = superuser_client.get(
        reverse("admin:clubs_organizer_change", args=[organizer.pk])
    ).content.decode()

    assert 'name="verified_at' not in page
    assert 'name="verified_by' not in page


def test_verify_action_verifies_and_records_event(
    superuser_client: Client, superuser: "_User"
) -> None:
    organizers = OrganizerFactory.create_batch(2)

    _run_action(superuser_client, "verify_selected", organizers)

    for organizer in organizers:
        organizer.refresh_from_db()
        assert organizer.is_verified is True
        assert organizer.verified_by == superuser
    events = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY)
    assert events.count() == 2
    assert {event.target_object_id for event in events} == {
        str(organizer.pk) for organizer in organizers
    }
    assert all(event.actor_user_id == superuser.pk for event in events)
    assert all(event.reason == REASON for event in events)


def test_unverify_action_clears_and_records_event(
    superuser_client: Client, superuser: "_User"
) -> None:
    organizer = OrganizerFactory.create()
    Organizer.objects.filter(pk=organizer.pk).update(
        verified_at=timezone.now(), verified_by=superuser
    )

    _run_action(superuser_client, "unverify_selected", [organizer])

    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert AuditEvent.objects.filter(action=AuditAction.ORGANIZER_UNVERIFY).count() == 1


def test_verify_action_twice_records_one_event(superuser_client: Client) -> None:
    organizer = OrganizerFactory.create()

    _run_action(superuser_client, "verify_selected", [organizer])
    _run_action(superuser_client, "verify_selected", [organizer])

    assert AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).count() == 1


def test_member_is_refused_and_warned_by_code(
    superuser_client: Client, superuser: "_User"
) -> None:
    own = OrganizerFactory.create(name="Made-up Person Name")
    other = OrganizerFactory.create()
    OrganizerMember.objects.create(
        organizer=own, account=superuser, role=MemberRole.ADMIN
    )

    messages = _run_action(superuser_client, "verify_selected", [own, other])

    own.refresh_from_db()
    other.refresh_from_db()
    assert own.is_verified is False
    assert other.is_verified is True
    assert AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).count() == 1
    text = " ".join(messages)
    assert own.public_code in text
    assert own.name not in text
    assert superuser.email not in text


def test_actions_hidden_without_verify_permission(client: Client) -> None:
    staff = _staff("view_organizer", "change_organizer")
    client.force_login(staff)

    page = client.get(reverse(ORGANIZER_CHANGELIST)).content.decode()

    assert "verify_selected" not in page
    assert "unverify_selected" not in page


def test_action_post_refused_without_verify_permission(client: Client) -> None:
    staff = _staff("view_organizer", "change_organizer")
    client.force_login(staff)
    organizer = OrganizerFactory.create()

    client.post(
        reverse(ORGANIZER_CHANGELIST),
        {
            "action": "verify_selected",
            "_selected_action": [str(organizer.pk)],
            "reason": REASON,
        },
    )

    organizer.refresh_from_db()
    assert organizer.is_verified is False
    assert not AuditEvent.objects.filter(action=AuditAction.ORGANIZER_VERIFY).exists()


def test_actions_shown_with_verify_permission(client: Client) -> None:
    staff = _staff("view_organizer", "change_organizer", "verify_organizer")
    client.force_login(staff)
    organizer = OrganizerFactory.create()

    page = client.get(reverse(ORGANIZER_CHANGELIST)).content.decode()
    assert "verify_selected" in page
    assert "unverify_selected" in page

    _run_action(client, "verify_selected", [organizer])
    organizer.refresh_from_db()
    assert organizer.verified_by == staff


def test_club_list_shows_code_not_only_name(superuser_client: Client) -> None:
    club = ClubFactory.create()

    page = superuser_client.get(reverse(CLUB_CHANGELIST)).content.decode()

    assert club.public_code in page
    assert Club.objects.count() == 1
