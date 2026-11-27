"""Club self-service rules in ``apps.clubs.club_self_service`` (#230).

Create, profile changes, admin invites, accepting, the admin list and
removal, and Club Organizer Grants, each checked at the service, plus
the Organizer side list in ``apps.clubs.self_service``. The API tests
(``test_club_self_service_api.py``) cover the answers. Made-up data only.
"""

import json
from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.club_self_service import (
    accept_club_invite,
    create_club,
    grant_organizer,
    invite_admin,
    list_admins,
    list_grants,
    remove_admin,
    remove_grant,
    update_club,
)
from apps.clubs.models import (
    Club,
    ClubInvite,
    ClubOrganizerGrant,
    ClubRole,
    ClubRoleKind,
    MemberRole,
)
from apps.clubs.self_service import (
    AddressNotVerified,
    Conflict,
    NotFound,
    list_club_grants,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubInviteFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.emails.models import QueuedEmail
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _account(
    *,
    mfa: bool = True,
    player: bool = True,
    verified: bool = True,
    email: str | None = None,
) -> User:
    account: User = (
        UserFactory.create() if email is None else UserFactory.create(email=email)
    )
    EmailAddress.objects.create(
        user=account, email=account.email, verified=verified, primary=True
    )
    if mfa:
        enroll_totp(account)
    if player:
        PlayerFactory.create(account=account)
    return account


def _admin_of(club: Club) -> User:
    account = _account()
    ClubRoleFactory.create(club=club, account=account)
    return account


def _events(action: str) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("pk"))


def _invite_key(club: Club, admin: User, email: str) -> str:
    invite_admin(club.pk, actor=admin, email=email)
    body = QueuedEmail.objects.filter(to=email).latest("pk").body
    return body.split("/clubs/invites/")[1].split()[0]


def _no_address(event: AuditEvent) -> None:
    assert "@" not in json.dumps([event.before, event.after, event.reason])


# Create


def test_create_makes_a_club_with_the_actor_as_only_admin() -> None:
    account = _account(mfa=False, player=False)

    club = create_club(
        actor=account, name="  Made-up Courts ", city="Made-up City", region="MU"
    )

    assert (club.name, club.city, club.region) == (
        "Made-up Courts",
        "Made-up City",
        "MU",
    )
    roles = list(ClubRole.objects.filter(club=club))
    assert [(r.account_id, r.role) for r in roles] == [(account.pk, ClubRoleKind.ADMIN)]
    [created] = _events(AuditAction.CLUB_CREATE)
    [added] = _events(AuditAction.CLUB_ROLE_ADD)
    assert created.actor_user_id == account.pk
    assert created.before is None
    assert created.after is None
    assert added.actor_user_id == account.pk


def test_create_refuses_an_unverified_login_address_and_writes_nothing() -> None:
    account = _account(verified=False)

    with pytest.raises(AddressNotVerified) as caught:
        create_club(actor=account, name="Made-up Courts")

    assert str(caught.value) == "Verify your email address before creating a club."
    assert not Club.objects.exists()
    assert not AuditEvent.objects.exists()


def test_create_refuses_past_the_per_account_limit(settings: Any) -> None:
    settings.CLUB_CREATE_LIMIT_PER_ACCOUNT = 2
    account = _account()
    create_club(actor=account, name="Made-up One")
    create_club(actor=account, name="Made-up Two")

    with pytest.raises(Conflict):
        create_club(actor=account, name="Made-up Three")

    assert Club.objects.count() == 2


def test_create_refuses_an_inactive_account() -> None:
    account = _account()
    User.objects.filter(pk=account.pk).update(is_active=False)

    with pytest.raises(NotFound):
        create_club(actor=account, name="Made-up Courts")

    assert not Club.objects.exists()


def test_create_refuses_a_blank_name_with_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        create_club(actor=_account(), name="   ")

    assert not Club.objects.exists()


# The admin rule


def test_every_admin_action_refuses_a_non_admin_and_an_admin_without_mfa() -> None:
    club = ClubFactory.create()
    outsider = _account()
    no_mfa = _account(mfa=False)
    ClubRoleFactory.create(club=club, account=no_mfa)
    inactive = _admin_of(club)
    User.objects.filter(pk=inactive.pk).update(is_active=False)
    inactive.refresh_from_db()
    organizer = OrganizerFactory.create()
    actions = [
        lambda actor: update_club(club.pk, actor=actor, name="Made-up New"),
        lambda actor: invite_admin(club.pk, actor=actor, email="x@example.com"),
        lambda actor: list_admins(club.pk, actor=actor),
        lambda actor: remove_admin(club.pk, "P-0000-0000", actor=actor),
        lambda actor: list_grants(club.pk, actor=actor),
        lambda actor: grant_organizer(club.pk, organizer.public_code, actor=actor),
        lambda actor: remove_grant(club.pk, organizer.public_code, actor=actor),
    ]

    for actor in (outsider, no_mfa, inactive):
        for action in actions:
            with pytest.raises(NotFound):
                action(actor)

    assert not ClubInvite.objects.exists()
    assert not ClubOrganizerGrant.objects.exists()
    assert not QueuedEmail.objects.exists()


def test_an_unknown_club_is_not_found() -> None:
    admin = _admin_of(ClubFactory.create())

    with pytest.raises(NotFound):
        update_club(0, actor=admin, name="Made-up New")


# Profile changes


def test_update_records_the_changed_fields_old_and_new() -> None:
    club = ClubFactory.create(name="Made-up Old", city="Made-up Town", region="AA")
    admin = _admin_of(club)

    changed = update_club(
        club.pk, actor=admin, name="Made-up New", city="Made-up Town", region="BB"
    )

    assert changed is True
    club.refresh_from_db()
    assert (club.name, club.city, club.region) == ("Made-up New", "Made-up Town", "BB")
    [event] = _events(AuditAction.CLUB_CHANGE)
    assert event.actor_user_id == admin.pk
    assert event.before == {"name": "Made-up Old", "region": "AA"}
    assert event.after == {"name": "Made-up New", "region": "BB"}


def test_an_unchanged_or_empty_update_records_nothing() -> None:
    club = ClubFactory.create(name="Made-up Same", city="")
    admin = _admin_of(club)

    assert update_club(club.pk, actor=admin, name=" Made-up Same ") is False
    assert update_club(club.pk, actor=admin) is False
    assert not _events(AuditAction.CLUB_CHANGE)


def test_a_bad_update_changes_nothing() -> None:
    club = ClubFactory.create(name="Made-up Kept")
    admin = _admin_of(club)

    with pytest.raises(ValidationError):
        update_club(club.pk, actor=admin, name="  ")

    club.refresh_from_db()
    assert club.name == "Made-up Kept"
    assert not _events(AuditAction.CLUB_CHANGE)


# Invites


def test_invite_stores_a_hash_and_queues_one_email_with_the_code() -> None:
    club = ClubFactory.create(name="Made-up Secret Courts")
    admin = _admin_of(club)

    key = _invite_key(club, admin, "new.made-up@example.com")

    invite = ClubInvite.objects.get()
    assert invite.email == "new.made-up@example.com"
    assert invite.invited_by_id == admin.pk
    assert key not in invite.key_hash
    assert invite.expires_at > timezone.now() + timedelta(days=6)
    [email] = QueuedEmail.objects.all()
    assert club.public_code in email.body
    for part in (email.subject, email.body, email.html):
        assert "Made-up Secret Courts" not in part
        assert admin.email not in part
    [event] = _events(AuditAction.CLUB_INVITE_CREATE)
    assert event.after == {"club": club.public_code}
    assert event.actor_user_id == admin.pk
    _no_address(event)


def test_inviting_an_admins_address_stores_and_sends_nothing() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other = _admin_of(club)

    invite_admin(club.pk, actor=admin, email=other.email.upper())

    assert not ClubInvite.objects.exists()
    assert not QueuedEmail.objects.exists()


def test_reinviting_replaces_the_earlier_invite() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    first = _invite_key(club, admin, "again.made-up@example.com")
    second = _invite_key(club, admin, "again.made-up@example.com")

    assert first != second
    assert ClubInvite.objects.count() == 1
    invitee = _account(email="again.made-up@example.com")
    with pytest.raises(NotFound):
        accept_club_invite(first, actor=invitee)


def test_open_invites_past_the_cap_are_refused(settings: Any) -> None:
    settings.CLUB_OPEN_INVITES_LIMIT = 1
    club = ClubFactory.create()
    admin = _admin_of(club)
    invite_admin(club.pk, actor=admin, email="a@example.com")
    # Replacing a still open invite adds none.
    invite_admin(club.pk, actor=admin, email="a@example.com")
    ClubInviteFactory.create(
        club=club,
        email="expired@example.com",
        expires_at=timezone.now() - timedelta(minutes=1),
    )

    for address in ("b@example.com", "expired@example.com"):
        with pytest.raises(Conflict):
            invite_admin(club.pk, actor=admin, email=address)

    assert ClubInvite.objects.filter(expires_at__gt=timezone.now()).count() == 1


# Accepting


def test_accept_makes_the_account_an_admin_and_deletes_the_invite() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    invitee = _account(mfa=False)
    key = _invite_key(club, admin, invitee.email)

    role = accept_club_invite(key, actor=invitee)

    assert (role.club_id, role.account_id, role.role) == (
        club.pk,
        invitee.pk,
        ClubRoleKind.ADMIN,
    )
    assert not ClubInvite.objects.exists()
    [added] = _events(AuditAction.CLUB_ROLE_ADD)
    assert added.actor_user_id == invitee.pk
    with pytest.raises(NotFound):
        accept_club_invite(key, actor=invitee)


def test_every_accept_refusal_is_not_found() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    intended = _account()
    key = _invite_key(club, admin, intended.email)
    unverified = _account(verified=False)
    unverified_key = _invite_key(club, admin, unverified.email)
    no_player = _account(player=False)
    no_player_key = _invite_key(club, admin, no_player.email)
    expired = _account()
    expired_key = _invite_key(club, admin, expired.email)
    ClubInvite.objects.filter(email=expired.email).update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )

    attempts = [
        (key, _account()),
        ("made-up-wrong", intended),
        ("x" * 500, intended),
        ("", intended),
        (unverified_key, unverified),
        (no_player_key, no_player),
        (expired_key, expired),
    ]
    for attempt_key, actor in attempts:
        with pytest.raises(NotFound):
            accept_club_invite(attempt_key, actor=actor)

    assert ClubRole.objects.count() == 1


def test_a_verified_secondary_address_does_not_count() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    invitee = _account()
    EmailAddress.objects.create(
        user=invitee, email="second.made-up@example.com", verified=True
    )
    key = _invite_key(club, admin, "second.made-up@example.com")

    with pytest.raises(NotFound):
        accept_club_invite(key, actor=invitee)


def test_an_invite_from_a_sender_who_lost_admin_authority_is_refused() -> None:
    club = ClubFactory.create()
    sender = _admin_of(club)
    _admin_of(club)
    lost_mfa = _account()
    key = _invite_key(club, sender, lost_mfa.email)
    Authenticator.objects.filter(user_id=sender.pk).delete()

    with pytest.raises(NotFound):
        accept_club_invite(key, actor=lost_mfa)

    enroll_totp(sender)
    removed_key = _invite_key(club, sender, "later.made-up@example.com")
    ClubRole.objects.filter(account=sender).delete()
    later = _account(email="later.made-up@example.com")
    with pytest.raises(NotFound):
        accept_club_invite(removed_key, actor=later)

    assert not ClubRole.objects.filter(account__in=[lost_mfa, later]).exists()


def test_accept_by_an_admin_is_a_conflict_and_uses_the_invite() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    invitee = _account()
    key = _invite_key(club, admin, invitee.email)
    ClubRoleFactory.create(club=club, account=invitee)

    with pytest.raises(Conflict):
        accept_club_invite(key, actor=invitee)

    assert not ClubInvite.objects.exists()


# Admins


def test_list_admins_shows_player_codes_and_names_never_an_email() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other = _admin_of(club)
    no_player = _account(player=False)
    ClubRoleFactory.create(club=club, account=no_player)

    views = list_admins(club.pk, actor=admin)

    with_players = sorted(
        [
            (admin.player.public_code, admin.player.display_name, True),
            (other.player.public_code, other.player.display_name, False),
        ]
    )
    expected = [*with_players, (None, None, False)]
    assert [(v.player, v.display_name, v.you) for v in views] == expected
    assert admin.email not in repr(views)


def test_remove_an_admin_records_it_and_deletes_their_sent_invites() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other = _admin_of(club)
    invite_admin(club.pk, actor=other, email="from.other@example.com")
    invite_admin(club.pk, actor=admin, email="from.admin@example.com")
    elsewhere = ClubInviteFactory.create(invited_by=other)

    remove_admin(club.pk, other.player.public_code, actor=admin)

    assert not ClubRole.objects.filter(account=other).exists()
    assert set(ClubInvite.objects.values_list("email", flat=True)) == {
        "from.admin@example.com",
        elsewhere.email,
    }
    [removed] = _events(AuditAction.CLUB_ROLE_REMOVE)
    assert removed.actor_user_id == admin.pk


def test_the_last_active_admin_cannot_be_removed() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    inactive = _account()
    ClubRoleFactory.create(club=club, account=inactive)
    User.objects.filter(pk=inactive.pk).update(is_active=False)

    with pytest.raises(Conflict) as caught:
        remove_admin(club.pk, admin.player.public_code, actor=admin)

    assert str(caught.value) == "A club must keep at least one admin."
    assert ClubRole.objects.filter(account=admin).exists()


def test_an_admin_may_remove_themselves_when_another_stays() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    _admin_of(club)

    remove_admin(club.pk, admin.player.public_code, actor=admin)

    assert not ClubRole.objects.filter(account=admin).exists()


def test_removing_a_player_who_is_not_an_admin_is_not_found() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    elsewhere = _admin_of(ClubFactory.create())

    for code in (elsewhere.player.public_code, "P-0000-0000"):
        with pytest.raises(NotFound):
            remove_admin(club.pk, code, actor=admin)


# Grants


def test_grant_list_and_remove_an_organizer() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    organizer = OrganizerFactory.create(name="Made-up Hidden Organizer")

    grant = grant_organizer(club.pk, organizer.public_code.lower(), actor=admin)

    assert (grant.club_id, grant.organizer_id) == (club.pk, organizer.pk)
    [view] = list_grants(club.pk, actor=admin)
    assert view.organizer == organizer.public_code
    assert view.created_at == grant.created_at
    assert "Made-up Hidden Organizer" not in repr(view)
    [added] = _events(AuditAction.CLUB_ORGANIZER_GRANT_ADD)
    assert added.actor_user_id == admin.pk

    remove_grant(club.pk, organizer.public_code, actor=admin)

    assert not ClubOrganizerGrant.objects.exists()
    [removed] = _events(AuditAction.CLUB_ORGANIZER_GRANT_REMOVE)
    assert removed.actor_user_id == admin.pk


def test_grant_refuses_an_unknown_or_malformed_organizer_and_a_repeat() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    organizer = OrganizerFactory.create()
    grant_organizer(club.pk, organizer.public_code, actor=admin)

    for code in ("O-0000-0000", "not-a-code", "", "x" * 500):
        with pytest.raises(NotFound):
            grant_organizer(club.pk, code, actor=admin)
    with pytest.raises(Conflict):
        grant_organizer(club.pk, organizer.public_code, actor=admin)

    assert ClubOrganizerGrant.objects.count() == 1


def test_removing_a_grant_that_does_not_exist_is_not_found() -> None:
    club = ClubFactory.create()
    admin = _admin_of(club)
    other_club_grant = ClubOrganizerGrantFactory.create()

    with pytest.raises(NotFound):
        remove_grant(club.pk, other_club_grant.organizer.public_code, actor=admin)

    assert ClubOrganizerGrant.objects.count() == 1


def test_the_organizer_side_lists_its_club_grants_for_an_admin_member() -> None:
    organizer = OrganizerFactory.create()
    member = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=member, role=MemberRole.ADMIN
    )
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)
    club = ClubFactory.create(name="Made-up Courts", city="Made-up City", region="MU")
    grant = ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    ClubOrganizerGrantFactory.create()

    [view] = list_club_grants(organizer.pk, actor=member)

    assert (view.club, view.name, view.city, view.region, view.created_at) == (
        club.public_code,
        "Made-up Courts",
        "Made-up City",
        "MU",
        grant.created_at,
    )
    for actor in (manager, _account()):
        with pytest.raises(NotFound):
            list_club_grants(organizer.pk, actor=actor)
