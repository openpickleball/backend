"""Organizer self-service rules in ``apps.clubs.self_service`` (#229).

Create, rename, invites, accepting, member roles and removal, and
verification requests, each checked at the service. The API tests
(``test_self_service_api.py``) cover the answers. Made-up data only.
"""

from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Permission
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.invites import hash_invite_key
from apps.clubs.models import (
    MemberRole,
    Organizer,
    OrganizerInvite,
    OrganizerMember,
    OrganizerVerificationRequest,
)
from apps.clubs.self_service import (
    AddressNotVerified,
    Conflict,
    NotFound,
    accept_invite,
    change_member_role,
    create_organizer,
    invite_member,
    list_members,
    remove_member,
    rename_organizer,
    request_verification,
)
from apps.clubs.services import verify_organizer
from apps.clubs.tests.factories import (
    OrganizerFactory,
    OrganizerInviteFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.emails.models import QueuedEmail
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _account(*, mfa: bool = True, player: bool = True) -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    if mfa:
        enroll_totp(account)
    if player:
        PlayerFactory.create(account=account)
    return account


def _admin_of(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("pk"))


def _invite_key(organizer: Organizer, admin: User, email: str, role: str) -> str:
    invite_member(organizer.pk, actor=admin, email=email, role=role)
    body = QueuedEmail.objects.filter(to=email).latest("pk").body
    return body.split("/organizer/invites/")[1].split()[0]


# Create


def test_create_makes_an_unverified_organizer_with_the_actor_as_only_admin() -> None:
    account = _account(mfa=False, player=False)

    organizer = create_organizer(actor=account, name="  Made-up Ladder  ")

    assert organizer.name == "Made-up Ladder"
    assert organizer.verified_at is None
    members = list(OrganizerMember.objects.filter(organizer=organizer))
    assert [(m.account_id, m.role) for m in members] == [(account.pk, MemberRole.ADMIN)]
    created = _events(AuditAction.ORGANIZER_CREATE)
    added = _events(AuditAction.ORGANIZER_MEMBER_ADD)
    assert len(created) == 1
    assert len(added) == 1
    assert created[0].actor_user_id == account.pk
    for event in created + added:
        assert "Made-up Ladder" not in str([event.before, event.after, event.reason])


def test_create_refuses_an_unverified_login_address_and_writes_nothing() -> None:
    account: User = UserFactory.create()
    EmailAddress.objects.create(user=account, email=account.email, verified=False)

    with pytest.raises(AddressNotVerified):
        create_organizer(actor=account, name="Made-up Ladder")

    assert not Organizer.objects.exists()
    assert not AuditEvent.objects.exists()


def test_create_refuses_past_the_per_account_limit(
    settings: object,
) -> None:
    settings.ORGANIZER_CREATE_LIMIT_PER_ACCOUNT = 2  # type: ignore[attr-defined]
    account = _account()
    create_organizer(actor=account, name="Made-up One")
    create_organizer(actor=account, name="Made-up Two")

    with pytest.raises(Conflict):
        create_organizer(actor=account, name="Made-up Three")

    assert Organizer.objects.count() == 2


def test_match_manager_memberships_do_not_count_toward_the_limit(
    settings: object,
) -> None:
    settings.ORGANIZER_CREATE_LIMIT_PER_ACCOUNT = 1  # type: ignore[attr-defined]
    account = _account()
    OrganizerMemberFactory.create(account=account, role=MemberRole.MATCH_MANAGER)

    create_organizer(actor=account, name="Made-up Own")


def test_created_organizer_gives_no_authority_until_verified() -> None:
    from apps.matches.permissions import can_manage_events

    account = _account()
    organizer = create_organizer(actor=account, name="Made-up Ladder")
    request_verification(organizer.pk, actor=account, statement="Weekly ladder.")

    assert can_manage_events(account, organizer.pk) is False
    verify(organizer)
    assert can_manage_events(account, organizer.pk) is True


# Rename


def test_rename_records_the_field_name_only() -> None:
    organizer = OrganizerFactory.create(name="Made-up Old")
    admin = _admin_of(organizer)

    assert rename_organizer(organizer.pk, actor=admin, name="Made-up New") is True

    organizer.refresh_from_db()
    assert organizer.name == "Made-up New"
    [event] = _events(AuditAction.ORGANIZER_CHANGE)
    assert event.after == {"fields": ["name"]}
    assert event.before is None


def test_an_unchanged_name_records_nothing() -> None:
    organizer = OrganizerFactory.create(name="Made-up Same")
    admin = _admin_of(organizer)

    assert rename_organizer(organizer.pk, actor=admin, name="Made-up Same") is False
    assert _events(AuditAction.ORGANIZER_CHANGE) == []


@pytest.mark.parametrize("role", [None, MemberRole.MATCH_MANAGER])
def test_rename_refuses_a_non_admin(role: MemberRole | None) -> None:
    organizer = OrganizerFactory.create(name="Made-up Kept")
    account = _account()
    if role is not None:
        OrganizerMemberFactory.create(organizer=organizer, account=account, role=role)

    with pytest.raises(NotFound):
        rename_organizer(organizer.pk, actor=account, name="Made-up Taken")

    organizer.refresh_from_db()
    assert organizer.name == "Made-up Kept"


def test_an_admin_without_mfa_is_refused() -> None:
    organizer = OrganizerFactory.create(name="Made-up Kept")
    account = _account(mfa=False)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN, mfa=False
    )

    with pytest.raises(NotFound):
        rename_organizer(organizer.pk, actor=account, name="Made-up Taken")


def test_rename_locks_the_organizer_first() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)

    with CaptureQueriesContext(connection) as queries:
        rename_organizer(organizer.pk, actor=admin, name="Made-up Locked")

    locks = [q["sql"] for q in queries if "FOR" in q["sql"]]
    assert "clubs_organizer" in locks[1]
    assert locks[1].endswith("FOR UPDATE")


# Invites


def test_invite_queues_one_email_with_the_link_and_stores_only_the_hash() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)

    key = _invite_key(organizer, admin, "invitee@example.com", MemberRole.ADMIN)

    invite = OrganizerInvite.objects.get()
    assert invite.email == "invitee@example.com"
    assert invite.key_hash == hash_invite_key(key)
    assert key not in str(OrganizerInvite.objects.values().get())
    assert invite.role == MemberRole.ADMIN
    assert invite.invited_by_id == admin.pk
    [email] = QueuedEmail.objects.filter(to="invitee@example.com")
    assert admin.email not in email.body
    assert admin.email not in email.html
    [event] = _events(AuditAction.ORGANIZER_INVITE_CREATE)
    assert event.after == {"organizer": organizer.public_code, "role": "admin"}
    assert "invitee@example.com" not in str([event.before, event.after])


def test_inviting_the_address_again_replaces_the_invite() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    first = _invite_key(organizer, admin, "again@example.com", MemberRole.ADMIN)

    second = _invite_key(
        organizer, admin, "again@example.com", MemberRole.MATCH_MANAGER
    )

    invite = OrganizerInvite.objects.get()
    assert invite.key_hash == hash_invite_key(second) != hash_invite_key(first)
    assert invite.role == MemberRole.MATCH_MANAGER


def test_inviting_a_member_stores_and_sends_nothing() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    member = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=member)

    invite_member(
        organizer.pk, actor=admin, email=member.email.upper(), role=MemberRole.ADMIN
    )

    assert not OrganizerInvite.objects.exists()
    assert not QueuedEmail.objects.filter(to=member.email).exists()


def test_the_open_invite_cap_refuses_a_new_address(settings: object) -> None:
    settings.ORGANIZER_OPEN_INVITES_LIMIT = 1  # type: ignore[attr-defined]
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invite_member(organizer.pk, actor=admin, email="one@example.com", role="admin")

    with pytest.raises(Conflict):
        invite_member(organizer.pk, actor=admin, email="two@example.com", role="admin")
    # The same address replaces its own invite, so it is not refused.
    invite_member(organizer.pk, actor=admin, email="one@example.com", role="admin")


def test_expired_invites_do_not_count_toward_the_cap(settings: object) -> None:
    settings.ORGANIZER_OPEN_INVITES_LIMIT = 1  # type: ignore[attr-defined]
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    OrganizerInviteFactory.create(
        organizer=organizer, expires_at=timezone.now() - timedelta(minutes=1)
    )

    invite_member(organizer.pk, actor=admin, email="fresh@example.com", role="admin")


def test_a_match_manager_cannot_invite() -> None:
    organizer = OrganizerFactory.create()
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    with pytest.raises(NotFound):
        invite_member(organizer.pk, actor=manager, email="x@example.com", role="admin")

    assert not OrganizerInvite.objects.exists()
    assert not QueuedEmail.objects.exists()


# Accept


def test_accept_adds_the_member_with_the_role_and_deletes_the_invite() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee = _account(mfa=False)
    key = _invite_key(organizer, admin, invitee.email, MemberRole.ADMIN)

    membership = accept_invite(key, actor=invitee)

    assert membership.organizer_id == organizer.pk
    assert membership.role == MemberRole.ADMIN
    assert not OrganizerInvite.objects.exists()
    [event] = _events(AuditAction.ORGANIZER_MEMBER_ADD)
    assert event.actor_user_id == invitee.pk


def test_accept_refuses_another_address() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    key = _invite_key(organizer, admin, "intended@example.com", MemberRole.ADMIN)

    with pytest.raises(NotFound):
        accept_invite(key, actor=_account())

    assert OrganizerInvite.objects.exists()


def test_accept_refuses_an_unverified_login_address() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee: User = UserFactory.create()
    PlayerFactory.create(account=invitee)
    EmailAddress.objects.create(user=invitee, email=invitee.email, verified=False)
    key = _invite_key(organizer, admin, invitee.email, MemberRole.ADMIN)

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)


def test_accept_refuses_an_account_without_a_player() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee = _account(player=False)
    key = _invite_key(organizer, admin, invitee.email, MemberRole.ADMIN)

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)


def test_accept_refuses_an_expired_or_unknown_key() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee = _account()
    key = _invite_key(organizer, admin, invitee.email, MemberRole.ADMIN)
    OrganizerInvite.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

    with pytest.raises(NotFound):
        accept_invite(key, actor=invitee)
    with pytest.raises(NotFound):
        accept_invite("made-up-unknown-key", actor=invitee)


def test_accept_by_a_member_is_a_conflict_and_deletes_the_invite() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    invitee = _account()
    key = _invite_key(organizer, admin, invitee.email, MemberRole.ADMIN)
    OrganizerMemberFactory.create(organizer=organizer, account=invitee)

    with pytest.raises(Conflict):
        accept_invite(key, actor=invitee)

    assert not OrganizerInvite.objects.exists()
    assert OrganizerMember.objects.get(account=invitee).role == (
        MemberRole.MATCH_MANAGER
    )


def test_the_verifier_cannot_accept_an_invite_to_the_organizer() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    verifier = _account()
    verifier.user_permissions.add(Permission.objects.get(codename="verify_organizer"))
    verify_organizer(organizer, actor=verifier)
    key = _invite_key(organizer, admin, verifier.email, MemberRole.ADMIN)

    with pytest.raises(NotFound):
        accept_invite(key, actor=verifier)

    assert not OrganizerMember.objects.filter(account=verifier).exists()
    assert OrganizerInvite.objects.exists()


# Members


def test_list_members_shows_player_codes_names_roles_and_you() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)
    no_player = _account(player=False)
    OrganizerMemberFactory.create(
        organizer=organizer, account=no_player, role=MemberRole.ADMIN
    )

    members = list_members(organizer.pk, actor=admin)

    assert [(m.player, m.display_name, m.role, m.you) for m in members] == [
        (admin.player.public_code, admin.player.display_name, "admin", True),
        (None, None, "admin", False),
        (
            manager.player.public_code,
            manager.player.display_name,
            "match_manager",
            False,
        ),
    ]


def test_an_inactive_members_display_name_is_hidden() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    gone = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=gone)
    User.objects.filter(pk=gone.pk).update(is_active=False)

    [_, shown] = list_members(organizer.pk, actor=admin)

    assert shown.player == gone.player.public_code
    assert shown.display_name is None


def test_change_role_and_remove() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    other = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=other)

    change_member_role(
        organizer.pk, other.player.public_code, actor=admin, role=MemberRole.ADMIN
    )
    assert OrganizerMember.objects.get(account=other).role == MemberRole.ADMIN
    remove_member(organizer.pk, other.player.public_code, actor=admin)

    assert not OrganizerMember.objects.filter(account=other).exists()
    assert len(_events(AuditAction.ORGANIZER_MEMBER_CHANGE)) == 1
    assert len(_events(AuditAction.ORGANIZER_MEMBER_REMOVE)) == 1


def test_the_last_admin_cannot_be_demoted_or_removed() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    code = admin.player.public_code

    with pytest.raises(Conflict):
        change_member_role(organizer.pk, code, actor=admin, role="match_manager")
    with pytest.raises(Conflict):
        remove_member(organizer.pk, code, actor=admin)

    assert OrganizerMember.objects.get(account=admin).role == MemberRole.ADMIN


def test_an_inactive_admin_does_not_count_as_the_one_kept() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    inactive = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=inactive, role=MemberRole.ADMIN
    )
    User.objects.filter(pk=inactive.pk).update(is_active=False)

    with pytest.raises(Conflict):
        remove_member(organizer.pk, admin.player.public_code, actor=admin)


def test_a_second_admin_may_leave() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    _admin_of(organizer)

    remove_member(organizer.pk, admin.player.public_code, actor=admin)

    assert not OrganizerMember.objects.filter(account=admin).exists()


def test_a_player_who_is_not_a_member_is_not_found() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    elsewhere = _account()
    OrganizerMemberFactory.create(account=elsewhere)

    with pytest.raises(NotFound):
        remove_member(organizer.pk, elsewhere.player.public_code, actor=admin)
    with pytest.raises(NotFound):
        change_member_role(
            organizer.pk, elsewhere.player.public_code, actor=admin, role="admin"
        )


def test_member_changes_lock_the_organizer_before_counting() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    other = _admin_of(organizer)

    with CaptureQueriesContext(connection) as queries:
        change_member_role(
            organizer.pk, other.player.public_code, actor=admin, role="match_manager"
        )

    sql = [q["sql"] for q in queries]
    lock = next(
        n for n, s in enumerate(sql) if "clubs_organizer" in s and "FOR UPDATE" in s
    )
    count = next(n for n, s in enumerate(sql) if "COUNT(" in s)
    assert lock < count


# Verification requests


def test_request_verification_records_the_code_only() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)

    request = request_verification(
        organizer.pk, actor=admin, statement="Made-up weekly ladder."
    )

    assert request.statement == "Made-up weekly ladder."
    assert request.is_open
    [event] = _events(AuditAction.ORGANIZER_VERIFICATION_REQUEST_CREATE)
    assert event.after == {"organizer": organizer.public_code}
    assert "ladder" not in str([event.before, event.after, event.reason])
    organizer.refresh_from_db()
    assert organizer.verified_at is None


def test_a_second_open_request_is_a_conflict() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    request_verification(organizer.pk, actor=admin, statement="First.")

    with pytest.raises(Conflict):
        request_verification(organizer.pk, actor=admin, statement="Second.")


def test_a_verified_organizer_cannot_request() -> None:
    organizer = OrganizerFactory.create()
    admin = _admin_of(organizer)
    verify(organizer)

    with pytest.raises(Conflict):
        request_verification(organizer.pk, actor=admin, statement="Again.")


def test_a_match_manager_cannot_request() -> None:
    organizer = OrganizerFactory.create()
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    with pytest.raises(NotFound):
        request_verification(organizer.pk, actor=manager, statement="No.")

    assert not OrganizerVerificationRequest.objects.exists()
