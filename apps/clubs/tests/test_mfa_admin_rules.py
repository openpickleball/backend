"""An Organizer keeps one admin with MFA (#232, ADR 0057).

Demoting or removing the last active admin with MFA through self-service
is refused (``Conflict``), and so is removing the last Club admin with
MFA. Removing TOTP is refused with 409 ``last_mfa_admin`` when it would
leave an Organizer the Account is an admin of with no other active admin
with MFA; it then removes and records nothing. Clubs get no TOTP
removal block. Made-up data only.
"""

from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator

from apps.accounts.mfa_views import LAST_MFA_ADMIN_MESSAGE
from apps.accounts.models import User
from apps.accounts.tests.factories import (
    UserFactory,
    enroll_totp,
    totp_code,
    totp_secret,
)
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs import club_self_service, self_service
from apps.clubs.models import ClubRole, MemberRole, OrganizerMember
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

TOTP_PATH = "account/authenticators/totp"
REFUSED = {
    "status": 409,
    "errors": [{"message": LAST_MFA_ADMIN_MESSAGE, "code": "last_mfa_admin"}],
}


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _account(*, mfa: bool = True) -> User:
    account: User = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    PlayerFactory.create(account=account)
    if mfa:
        enroll_totp(account)
    return account


def _admin(organizer: Any, *, mfa: bool = True) -> User:
    account = _account(mfa=mfa)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN, mfa=mfa
    )
    return account


# Self-service counts only admins with MFA


def test_demoting_the_last_admin_with_mfa_is_refused() -> None:
    organizer = OrganizerFactory.create()
    actor = _admin(organizer)
    _admin(organizer, mfa=False)

    with pytest.raises(self_service.Conflict):
        self_service.change_member_role(
            organizer.pk,
            actor.player.public_code,
            actor=actor,
            role=MemberRole.MATCH_MANAGER,
        )

    assert OrganizerMember.objects.get(account=actor).role == MemberRole.ADMIN


def test_removing_the_last_admin_with_mfa_is_refused() -> None:
    organizer = OrganizerFactory.create()
    actor = _admin(organizer)
    _admin(organizer, mfa=False)

    with pytest.raises(self_service.Conflict):
        self_service.remove_member(organizer.pk, actor.player.public_code, actor=actor)

    assert OrganizerMember.objects.filter(account=actor).exists()


def test_another_admin_with_mfa_lets_an_admin_leave() -> None:
    organizer = OrganizerFactory.create()
    actor = _admin(organizer)
    _admin(organizer)

    self_service.remove_member(organizer.pk, actor.player.public_code, actor=actor)

    assert not OrganizerMember.objects.filter(account=actor).exists()


def test_removing_the_last_club_admin_with_mfa_is_refused() -> None:
    club = ClubFactory.create()
    actor = _account()
    ClubRoleFactory.create(club=club, account=actor)
    ClubRoleFactory.create(club=club, account=_account(mfa=False))

    with pytest.raises(self_service.Conflict):
        club_self_service.remove_admin(club.pk, actor.player.public_code, actor=actor)

    assert ClubRole.objects.filter(account=actor).exists()


# TOTP removal


def _signed_in(account: User, base: str) -> HeadlessSession:
    session = HeadlessSession(base)
    code = totp_code(totp_secret(account))
    assert session.mfa_login(account.email, code).status_code == 200
    assert session.reauthenticate().status_code == 200
    return session


def _mfa_events(account: User) -> int:
    return AuditEvent.objects.filter(
        action=AuditAction.MFA_CHANGE, actor_user=account
    ).count()


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_the_last_mfa_admin_cannot_remove_totp(base: str) -> None:
    organizer = OrganizerFactory.create()
    actor = _admin(organizer)
    _admin(organizer, mfa=False)
    session = _signed_in(actor, base)

    response = session.delete(TOTP_PATH)

    assert response.status_code == 409
    assert response.json() == REFUSED
    assert Authenticator.objects.filter(user=actor, type="totp").exists()
    assert Authenticator.objects.filter(user=actor, type="recovery_codes").exists()
    assert _mfa_events(actor) == 0


def test_two_mfa_admins_one_removes_then_the_other_is_refused() -> None:
    organizer = OrganizerFactory.create()
    first = _admin(organizer)
    second = _admin(organizer)
    first_session = _signed_in(first, APP)
    second_session = _signed_in(second, APP)

    assert first_session.delete(TOTP_PATH).status_code == 200
    refused = second_session.delete(TOTP_PATH)

    assert refused.status_code == 409
    assert refused.json() == REFUSED
    assert Authenticator.objects.filter(user=second, type="totp").exists()


def test_a_match_manager_may_remove_totp() -> None:
    organizer = OrganizerFactory.create()
    _admin(organizer)
    manager = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=manager)
    session = _signed_in(manager, APP)

    assert session.delete(TOTP_PATH).status_code == 200


def test_an_inactive_other_admin_does_not_count() -> None:
    organizer = OrganizerFactory.create()
    actor = _admin(organizer)
    other = _admin(organizer)
    other.is_active = False
    other.save()
    session = _signed_in(actor, APP)

    assert session.delete(TOTP_PATH).status_code == 409


def test_the_last_mfa_club_admin_may_remove_totp() -> None:
    club = ClubFactory.create()
    actor = _account()
    ClubRoleFactory.create(club=club, account=actor)
    session = _signed_in(actor, APP)

    assert session.delete(TOTP_PATH).status_code == 200
