"""Account deletion removes the Account's personal data (#116, ADR 0048).

``delete_account`` keeps only what ADR 0009's Retention section keeps:
a Player in the Ledger with its OPEN Code and its personal data cleared,
the Matches and Ratings, the Audit Events, and a cleared Account row when
something protects it. Made-up data only.
"""

import json
from datetime import timedelta
from importlib import import_module
from typing import TYPE_CHECKING

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.utils import timezone
from oauth2_provider.models import get_access_token_model

from apps.accounts.deletion import (
    DELETED_EMAIL_DOMAIN,
    DELETED_PLAYER_NAME,
    REMOVED_ORGANIZER_NAME,
    AccountAlreadyDeleted,
    delete_account,
)
from apps.accounts.models import Agreement, AgreementAcceptance
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import ClubRole, OrganizerMember, OrganizerReport
from apps.clubs.reports import RULE_REPORTER_DELETED, report_organizer
from apps.clubs.tests.factories import (
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.emails.models import QueuedEmail
from apps.matches.models import Match, MatchParticipant, Team
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db


def _account_with_rows() -> "_User":
    """An Account with an address row and accepted agreements."""
    account = UserFactory.create()
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    for agreement in Agreement:
        AgreementAcceptance.objects.create(
            account=account, agreement=agreement, version=1, accepted_at=timezone.now()
        )
    return account


def _played(player: Player) -> Match:
    others = [PlayerFactory.create() for _ in range(3)]
    match, _ = saved_match(players={Team.A: [player, others[0]], Team.B: others[1:]})
    return match


def _deletion_event(pk: int) -> AuditEvent:
    return AuditEvent.objects.get(
        action=AuditAction.ACCOUNT_DELETE,
        target_content_type=ContentType.objects.get_for_model(get_user_model()),
        target_object_id=str(pk),
    )


def test_an_account_nothing_protects_is_hard_deleted() -> None:
    account = _account_with_rows()
    player = PlayerFactory.create(account=account)
    pk = account.pk

    result = delete_account(account, actor=account)

    assert result.account_kept is False
    assert result.player_kept is False
    assert not get_user_model().objects.filter(pk=pk).exists()
    assert not Player.objects.filter(pk=player.pk).exists()
    assert not PlayerPrivacy.objects.filter(player_id=player.pk).exists()
    assert not EmailAddress.objects.filter(user_id=pk).exists()
    assert not AgreementAcceptance.objects.filter(account_id=pk).exists()
    event = _deletion_event(pk)
    assert event.actor_user_id is None
    assert event.after == {"account_kept": False, "player_kept": False}


def test_a_player_in_the_ledger_is_kept_with_its_personal_data_cleared() -> None:
    account = _account_with_rows()
    player = PlayerFactory.create(account=account, legal_name="Made Up Name")
    match = _played(player)
    pk, code = account.pk, player.public_code

    result = delete_account(account, actor=account)

    assert result.account_kept is True
    assert result.player_kept is True
    kept = Player.objects.get(pk=player.pk)
    assert kept.public_code == code
    assert kept.display_name == DELETED_PLAYER_NAME
    assert kept.legal_name == ""
    assert kept.birth_year is None
    assert kept.birth_month is None
    assert kept.gender_division == ""
    assert (kept.city, kept.region) == ("", "")
    privacy = PlayerPrivacy.objects.get(player=kept)
    assert {getattr(privacy, name) for name in PlayerPrivacy.SETTINGS} == {
        Visibility.PRIVATE
    }
    assert MatchParticipant.objects.filter(match=match, player=kept).exists()

    row = get_user_model().objects.get(pk=pk)
    assert row.email.endswith(f"@{DELETED_EMAIL_DOMAIN}")
    assert not row.has_usable_password()
    assert row.is_active is False
    assert (row.is_staff, row.is_superuser, row.last_login) == (False, False, None)
    assert not EmailAddress.objects.filter(user_id=pk).exists()
    assert not AgreementAcceptance.objects.filter(account_id=pk).exists()
    assert _deletion_event(pk).after == {"account_kept": True, "player_kept": True}


def test_no_audit_event_holds_the_deleted_values() -> None:
    account = _account_with_rows()
    player = PlayerFactory.create(account=account, legal_name="Made Up Name")
    _played(player)
    values = [account.email, player.display_name, player.legal_name, player.city]

    delete_account(account, actor=account)

    for event in AuditEvent.objects.all():
        stored = json.dumps([event.before, event.after, event.reason])
        for value in values:
            assert value not in stored


def test_a_kept_account_loses_its_groups_and_permissions() -> None:
    account = UserFactory.create()
    player = PlayerFactory.create(account=account)
    _played(player)
    account.groups.create(name="Made-up group")

    delete_account(account, actor=account)

    row = get_user_model().objects.get(pk=account.pk)
    assert not row.groups.exists()
    assert not row.user_permissions.exists()


def test_an_account_that_owns_a_partner_application_is_kept() -> None:
    account = UserFactory.create()
    application = PartnerApplicationFactory.create(user=account)

    result = delete_account(account, actor=account)

    assert result.account_kept is True
    application.refresh_from_db()
    assert application.user_id == account.pk


def test_sessions_and_their_rows_are_deleted() -> None:
    account = UserFactory.create()
    other = UserFactory.create()
    store_class = import_module(settings.SESSION_ENGINE).SessionStore
    keys = {}
    for owner in (account, other):
        store = store_class()
        store["made_up"] = 1
        store.create()
        UserSession.objects.create(
            user=owner, session_key=store.session_key, ip="192.0.2.1", user_agent="x"
        )
        keys[owner.pk] = store.session_key

    delete_account(account, actor=account)

    assert not Session.objects.filter(session_key=keys[account.pk]).exists()
    assert not UserSession.objects.filter(user_id=account.pk).exists()
    assert Session.objects.filter(session_key=keys[other.pk]).exists()
    assert UserSession.objects.filter(user_id=other.pk).exists()


def test_mfa_authenticators_and_oauth_tokens_are_deleted() -> None:
    account = UserFactory.create()
    enroll_totp(account)
    get_access_token_model().objects.create(
        user=account,
        token="made-up-token-for-tests",
        expires=timezone.now() + timedelta(hours=1),
        scope="read",
    )

    delete_account(account, actor=account)

    assert not Authenticator.objects.filter(user_id=account.pk).exists()
    assert not get_access_token_model().objects.filter(user_id=account.pk).exists()


def test_the_only_member_organizer_loses_its_name() -> None:
    account = UserFactory.create()
    alone = OrganizerFactory.create(name="Made Up Solo Organizer")
    shared = OrganizerFactory.create(name="Made Up Shared Organizer")
    OrganizerMemberFactory.create(organizer=alone, account=account, mfa=False)
    OrganizerMemberFactory.create(organizer=shared, account=account, mfa=False)
    OrganizerMemberFactory.create(organizer=shared, mfa=False)

    delete_account(account, actor=account)

    alone.refresh_from_db()
    shared.refresh_from_db()
    assert alone.name == REMOVED_ORGANIZER_NAME
    assert shared.name == "Made Up Shared Organizer"
    assert not OrganizerMember.objects.filter(account_id=account.pk).exists()
    renamed = AuditEvent.objects.get(action=AuditAction.ORGANIZER_NAME_REMOVE)
    assert renamed.target_object_id == str(alone.pk)
    assert renamed.after == {"fields": ["name"]}
    removed = AuditEvent.objects.filter(action=AuditAction.ORGANIZER_MEMBER_REMOVE)
    assert removed.count() == 2
    assert {event.actor_user_id for event in removed} == {None}


def test_club_roles_are_removed_with_an_audit_event() -> None:
    account = UserFactory.create()
    ClubRoleFactory.create(account=account)

    delete_account(account, actor=account)

    assert not ClubRole.objects.filter(account_id=account.pk).exists()
    assert AuditEvent.objects.filter(action=AuditAction.CLUB_ROLE_REMOVE).count() == 1


def test_a_staff_actor_is_the_actor_of_the_events() -> None:
    staff = UserFactory.create(is_staff=True)
    account = UserFactory.create()
    ClubRoleFactory.create(account=account)

    delete_account(account, actor=staff)

    removed = AuditEvent.objects.get(action=AuditAction.CLUB_ROLE_REMOVE)
    assert removed.actor_user_id == staff.pk
    assert _deletion_event(account.pk).actor_user_id == staff.pk


def test_the_accounts_reports_are_closed_then_deleted() -> None:
    reporter = PlayerFactory.create().account
    organizer = verified_organizer()
    report = report_organizer(organizer.pk, actor=reporter, reason="other")
    other = report_organizer(
        organizer.pk, actor=PlayerFactory.create().account, reason="other"
    )

    delete_account(reporter, actor=reporter)

    assert not OrganizerReport.objects.filter(pk=report.pk).exists()
    assert OrganizerReport.objects.filter(pk=other.pk).exists()
    closed = AuditEvent.objects.get(action=AuditAction.ORGANIZER_REPORT_CLOSE)
    assert closed.target_object_id == str(report.pk)
    assert closed.after is not None
    assert closed.after["rule"] == RULE_REPORTER_DELETED
    assert closed.actor_user_id is None
    # The create event names the reporter and stays (append-only).
    assert AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_REPORT_CREATE, actor_user_id=reporter.pk
    ).exists()


def test_queued_emails_to_its_addresses_are_deleted() -> None:
    account = _account_with_rows()
    EmailAddress.objects.create(
        user=account, email="other.address@example.org", verified=False
    )

    def queued(to: str) -> QueuedEmail:
        return QueuedEmail.objects.create(
            template="made_up", from_email="open@example.org", to=to, body="x"
        )

    mine = [queued(account.email.upper()), queued("other.address@example.org")]
    theirs = queued("someone.else@example.org")

    delete_account(account, actor=account)

    assert not QueuedEmail.objects.filter(pk__in=[q.pk for q in mine]).exists()
    assert QueuedEmail.objects.filter(pk=theirs.pk).exists()


def test_admin_log_entries_tied_to_the_account_are_deleted() -> None:
    account = UserFactory.create()
    player = PlayerFactory.create(account=account)
    _played(player)
    staff = UserFactory.create(is_staff=True)
    other = UserFactory.create()

    def logged(by: "_User", obj: "_User | Player") -> LogEntry:
        return LogEntry.objects.create(
            user=by,
            content_type=ContentType.objects.get_for_model(type(obj)),
            object_id=str(obj.pk),
            object_repr=str(obj),
            action_flag=ADDITION,
        )

    tied = [logged(staff, account), logged(staff, player), logged(account, other)]
    kept = logged(staff, other)

    delete_account(account, actor=account)

    assert not LogEntry.objects.filter(pk__in=[e.pk for e in tied]).exists()
    assert LogEntry.objects.filter(pk=kept.pk).exists()


def test_an_already_deleted_account_is_refused() -> None:
    account = UserFactory.create()
    player = PlayerFactory.create(account=account)
    _played(player)
    delete_account(account, actor=account)
    kept = get_user_model().objects.get(pk=account.pk)

    with pytest.raises(AccountAlreadyDeleted):
        delete_account(kept, actor=None)

    assert AuditEvent.objects.filter(action=AuditAction.ACCOUNT_DELETE).count() == 1


def test_a_deactivated_account_is_still_erased() -> None:
    staff = UserFactory.create(is_staff=True)
    account = _account_with_rows()
    account.is_active = False
    account.save(update_fields=["is_active"])

    result = delete_account(account, actor=staff)

    assert result.account_kept is False
    assert not get_user_model().objects.filter(pk=account.pk).exists()


def test_a_member_with_a_report_on_its_own_organizer_is_deleted() -> None:
    reporter = PlayerFactory.create().account
    organizer = verified_organizer()
    OrganizerMemberFactory.create(organizer=organizer, account=reporter, mfa=False)
    report = report_organizer(organizer.pk, actor=reporter, reason="other")

    delete_account(reporter, actor=reporter)

    assert not OrganizerReport.objects.filter(pk=report.pk).exists()
    assert not OrganizerMember.objects.filter(account_id=reporter.pk).exists()


def test_no_player_is_created_for_an_inactive_account() -> None:
    # A Player create that waited on a deletion's Account lock reads the
    # stored row after the deletion committed: an inactive Account.
    account = UserFactory.create()
    get_user_model().objects.filter(pk=account.pk).update(is_active=False)

    with pytest.raises(ValidationError) as raised:
        PlayerFactory.create(account=account)

    assert "account" in raised.value.message_dict
    assert not Player.objects.filter(account_id=account.pk).exists()


def test_an_active_account_with_a_placeholder_like_email_is_still_deleted() -> None:
    account = UserFactory.create(email=f"deleted-made-up@{DELETED_EMAIL_DOMAIN}")

    result = delete_account(account, actor=account)

    assert result.account_kept is False
