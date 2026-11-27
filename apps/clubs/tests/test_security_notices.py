"""Security notices to Organizer and Club admins (#232, ADR 0057).

Each change to who may act for an Organizer or Club, or to its
verification, queues one email per other active admin once the change
commits: never before, never for a rolled-back change, never to the
actor or the Account the change is about. A notice names OPEN Codes, a
role, and a hold's end, never an address, a person's name, an
Organizer's or Club's name, or a client id. Made-up data only.
"""

import logging
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.db import transaction
from django.utils import timezone

from apps.accounts.deletion import delete_account
from apps.accounts.models import User
from apps.accounts.tests.factories import (
    UserFactory,
    enroll_totp,
    totp_code,
    totp_secret,
)
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession
from apps.audit.access import (
    delete_access_row,
    record_cascaded_removals,
    save_access_row,
)
from apps.clubs import club_self_service, notices, self_service
from apps.clubs.emails import SECURITY_NOTICE, render_security_notice
from apps.clubs.models import Club, ClubRoleKind, MemberRole, Organizer, OrganizerMember
from apps.clubs.services import end_member_holds, unverify_organizer, verify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verify,
)
from apps.emails.models import QueuedEmail
from apps.emails.tests.hygiene import assert_clean_message, assert_clean_template
from apps.matches.permissions import MODERATOR_GROUP
from apps.partners.models import OrganizerApplicationGrant
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

TEMPLATES = Path(settings.BASE_DIR) / "templates" / "clubs" / "email"


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _account(*, mfa: bool = True) -> User:
    account: User = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    if mfa:
        enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _admin(organizer: Organizer) -> User:
    account = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _club_admin(club: Club, *, mfa: bool = True) -> User:
    account = _account(mfa=mfa)
    ClubRoleFactory.create(club=club, account=account)
    return account


def _notices() -> list[QueuedEmail]:
    return list(QueuedEmail.objects.filter(template=SECURITY_NOTICE).order_by("pk"))


def _recipients() -> set[str]:
    return {row.to for row in _notices()}


def _committed(capture: Any, change: Callable[[], object]) -> None:
    """Run ``change`` and its on_commit callbacks, checking that nothing
    was queued before they ran."""
    with capture(execute=False) as callbacks:
        change()
    assert _notices() == []
    for callback in callbacks:
        callback()


class _Abort(Exception):
    pass


def with_rollback(change: Callable[[], None]) -> None:
    """Run ``change``, which raises ``_Abort`` to roll itself back."""
    with pytest.raises(_Abort):
        change()


def _holds_nothing_identifying(*values: str) -> None:
    for row in _notices():
        for part in (row.subject, row.body, row.html):
            for value in values:
                assert value not in part


# Templates


def test_the_templates_are_clean() -> None:
    files = sorted(TEMPLATES.glob("security_notice_*"))

    assert [f.name for f in files] == [
        "security_notice_message.html",
        "security_notice_message.txt",
        "security_notice_subject.txt",
    ]
    for path in files:
        assert_clean_template(path)


@pytest.mark.parametrize("kind", list(notices.NoticeKind))
def test_every_kind_renders_clean(kind: notices.NoticeKind) -> None:
    notice = notices.SecurityNotice(
        kind=kind,
        scope=notices.ORGANIZER,
        scope_id=1,
        code="G-MADE-UP1",
        role=MemberRole.ADMIN,
        organizer_code="G-MADE-UP2",
        hold_ends_at=timezone.now(),
    )

    message = render_security_notice(
        "made.up.admin@example.com",
        scope=notice.scope,
        code=notice.code,
        summary=notice.summary(),
        follow_up=notice.follow_up(),
    )

    assert_clean_message(message)
    assert "G-MADE-UP1" in message.subject
    assert "{" not in str(message.body)


# Organizer Members


def test_an_accepted_invite_tells_the_other_admins_with_the_hold(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = verify(OrganizerFactory.create())
    sender, other = _admin(organizer), _admin(organizer)
    invitee = _account()
    self_service.invite_member(
        organizer.pk, actor=sender, email=invitee.email, role=MemberRole.ADMIN
    )
    body = QueuedEmail.objects.filter(to=invitee.email).latest("pk").body
    key = body.split("/organizer/invites/")[1].split()[0]

    _committed(
        django_capture_on_commit_callbacks,
        lambda: self_service.accept_invite(key, actor=invitee),
    )

    assert _recipients() == {sender.email, other.email}
    member = OrganizerMember.objects.get(account=invitee)
    assert member.hold_ends_at is not None
    hold = member.hold_ends_at.strftime("%Y-%m-%d %H:%M UTC")
    for row in _notices():
        assert organizer.public_code in row.subject
        assert f"organizer {organizer.public_code} as admin" in row.body
        assert hold in row.body
    _holds_nothing_identifying(
        invitee.email,
        sender.email,
        organizer.name,
        invitee.player.display_name,
    )


def test_a_role_change_skips_the_actor_and_the_member(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    actor, other = _admin(organizer), _admin(organizer)
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    _committed(
        django_capture_on_commit_callbacks,
        lambda: self_service.change_member_role(
            organizer.pk,
            manager.player.public_code,
            actor=actor,
            role=MemberRole.ADMIN,
        ),
    )

    assert _recipients() == {other.email}
    [row] = _notices()
    assert "was changed to admin" in row.body


def test_a_removal_tells_the_other_admins(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    actor, other = _admin(organizer), _admin(organizer)
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    _committed(
        django_capture_on_commit_callbacks,
        lambda: self_service.remove_member(
            organizer.pk, manager.player.public_code, actor=actor
        ),
    )

    assert _recipients() == {other.email}
    assert "was removed from organizer" in _notices()[0].body


def test_an_inactive_admin_gets_nothing(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    actor, inactive = _admin(organizer), _admin(organizer)
    inactive.is_active = False
    inactive.save()
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    with django_capture_on_commit_callbacks(execute=True):
        self_service.remove_member(
            organizer.pk, manager.player.public_code, actor=actor
        )

    assert _notices() == []


def test_a_rolled_back_change_queues_nothing(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer)
    account = _account()

    def change() -> None:
        with transaction.atomic():
            save_access_row(
                OrganizerMember(
                    organizer=organizer, account=account, role=MemberRole.ADMIN
                ),
                actor=account,
            )
            raise _Abort

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with_rollback(change)

    assert callbacks == []
    assert _notices() == []


def test_ending_a_hold_sends_nothing(django_capture_on_commit_callbacks: Any) -> None:
    organizer = verify(OrganizerFactory.create())
    _admin(organizer)
    OrganizerMemberFactory.create(
        organizer=organizer, hold_ends_at=timezone.now() + timedelta(hours=3)
    )
    moderator: User = UserFactory.create(is_staff=True)
    moderator.groups.add(Group.objects.get(name=MODERATOR_GROUP))

    with django_capture_on_commit_callbacks(execute=True):
        assert end_member_holds(organizer, actor=moderator, reason="Made up.") == 1

    assert _notices() == []


def test_a_cascaded_removal_sends_nothing(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club = ClubFactory.create()
    _club_admin(club)
    _club_admin(club)

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        record_cascaded_removals([club], actor=None)
        club.delete()

    assert _notices() == []


def test_account_deletion_tells_the_other_admins(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    other = _admin(organizer)
    leaving = _admin(organizer)
    club = ClubFactory.create()
    club_other = _club_admin(club)
    ClubRoleFactory.create(club=club, account=leaving)

    _committed(
        django_capture_on_commit_callbacks,
        lambda: delete_account(leaving, actor=leaving),
    )

    assert _recipients() == {other.email, club_other.email}


# Clubs


def test_a_club_admin_added_and_removed(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club = ClubFactory.create()
    sender, other = _club_admin(club), _club_admin(club)
    invitee = _account()
    club_self_service.invite_admin(club.pk, actor=sender, email=invitee.email)
    body = QueuedEmail.objects.filter(to=invitee.email).latest("pk").body
    key = body.split("/clubs/invites/")[1].split()[0]

    _committed(
        django_capture_on_commit_callbacks,
        lambda: club_self_service.accept_club_invite(key, actor=invitee),
    )

    assert _recipients() == {sender.email, other.email}
    assert all("An admin was added to club" in row.body for row in _notices())
    QueuedEmail.objects.all().delete()

    _committed(
        django_capture_on_commit_callbacks,
        lambda: club_self_service.remove_admin(
            club.pk, invitee.player.public_code, actor=sender
        ),
    )

    assert _recipients() == {other.email}
    _holds_nothing_identifying(club.name, invitee.email)


def test_a_club_grant_made_and_removed_names_the_organizer_code(
    django_capture_on_commit_callbacks: Any,
) -> None:
    club = ClubFactory.create()
    actor, other = _club_admin(club), _club_admin(club)
    organizer = OrganizerFactory.create()

    _committed(
        django_capture_on_commit_callbacks,
        lambda: club_self_service.grant_organizer(
            club.pk, organizer.public_code, actor=actor
        ),
    )

    assert _recipients() == {other.email}
    assert organizer.public_code in _notices()[0].body
    QueuedEmail.objects.all().delete()

    _committed(
        django_capture_on_commit_callbacks,
        lambda: club_self_service.remove_grant(
            club.pk, organizer.public_code, actor=actor
        ),
    )

    assert _recipients() == {other.email}
    assert "no longer allows" in _notices()[0].body
    _holds_nothing_identifying(club.name, organizer.name)


def test_an_application_grant_tells_the_organizer_admins(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    admin = _admin(organizer)
    application = PartnerApplicationFactory.create()
    staff: User = UserFactory.create(is_staff=True, is_superuser=True)
    grant = OrganizerApplicationGrant(organizer=organizer, application=application)

    _committed(
        django_capture_on_commit_callbacks, lambda: save_access_row(grant, actor=staff)
    )

    assert _recipients() == {admin.email}
    assert "partner application was given permission" in _notices()[0].body
    _holds_nothing_identifying(application.client_id, application.name)
    QueuedEmail.objects.all().delete()

    _committed(
        django_capture_on_commit_callbacks,
        lambda: delete_access_row(grant, actor=staff),
    )

    assert _recipients() == {admin.email}


# Verification


def test_verify_and_unverify_tell_every_admin(
    django_capture_on_commit_callbacks: Any,
) -> None:
    organizer = OrganizerFactory.create()
    first, second = _admin(organizer), _admin(organizer)
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )

    _committed(
        django_capture_on_commit_callbacks,
        lambda: verify_organizer(organizer, actor=moderator, reason="Made up."),
    )

    assert _recipients() == {first.email, second.email}
    assert "was verified" in _notices()[0].body
    QueuedEmail.objects.all().delete()

    with django_capture_on_commit_callbacks(execute=True):
        verify_organizer(organizer, actor=moderator, reason="Again.")
    assert _notices() == []

    _committed(
        django_capture_on_commit_callbacks,
        lambda: unverify_organizer(organizer, actor=moderator, reason="Made up."),
    )

    assert _recipients() == {first.email, second.email}
    assert "no longer verified" in _notices()[0].body


# MFA removal


def test_removing_the_last_method_tells_the_other_admins(
    commit_after_each_request: None,
) -> None:
    organizer = OrganizerFactory.create()
    other = _admin(organizer)
    leaving = _admin(organizer)
    club = ClubFactory.create()
    club_other = _club_admin(club)
    ClubRoleFactory.create(club=club, account=leaving, role=ClubRoleKind.ADMIN)
    session = HeadlessSession(APP)
    assert (
        session.mfa_login(leaving.email, totp_code(totp_secret(leaving))).status_code
        == 200
    )
    assert session.reauthenticate().status_code == 200

    mail.outbox.clear()

    response = session.delete("account/authenticators/totp")

    assert response.status_code == 200
    sent = [m for m in mail.outbox if m.subject.startswith("Security notice")]
    assert {address for m in sent for address in m.to} == {
        other.email,
        club_other.email,
    }
    assert all("removed their last two-factor method" in str(m.body) for m in sent)


def test_a_refused_removal_sends_nothing(commit_after_each_request: None) -> None:
    organizer = OrganizerFactory.create()
    leaving = _admin(organizer)
    without = _account(mfa=False)
    OrganizerMemberFactory.create(
        organizer=organizer, account=without, role=MemberRole.ADMIN, mfa=False
    )
    session = HeadlessSession(APP)
    session.mfa_login(leaving.email, totp_code(totp_secret(leaving)))
    session.reauthenticate()

    mail.outbox.clear()

    assert session.delete("account/authenticators/totp").status_code == 409
    assert not [m for m in mail.outbox if m.subject.startswith("Security notice")]


# Failures


def test_a_failed_notice_is_logged_by_id_and_never_fails_the_change(
    django_capture_on_commit_callbacks: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    organizer = OrganizerFactory.create()
    actor, other = _admin(organizer), _admin(organizer)
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError(other.email)

    monkeypatch.setattr(notices, "render_security_notice", broken)

    with (
        caplog.at_level(logging.WARNING, logger="apps.clubs.notices"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        self_service.remove_member(
            organizer.pk, manager.player.public_code, actor=actor
        )

    assert not OrganizerMember.objects.filter(account=manager).exists()
    assert _notices() == []
    assert f"organizer {organizer.pk} (RuntimeError)" in caplog.text
    assert other.email not in caplog.text
