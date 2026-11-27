"""A held admin does not count as the admin who stays (#232 review).

An admin whose New Member Hold is in force cannot act, so it must not
count as the remaining admin with MFA when another admin demotes or
removes itself, or removes its last TOTP. Made-up data only.
"""

from datetime import timedelta
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import (
    UserFactory,
    enroll_totp,
    totp_code,
    totp_secret,
)
from apps.accounts.tests.headless import APP, PASSWORD, HeadlessSession
from apps.clubs import self_service
from apps.clubs.models import MemberRole, Organizer, OrganizerMember
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory, verify
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _account() -> User:
    account: User = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    enroll_totp(account)
    PlayerFactory.create(account=account)
    return account


def _setup() -> tuple[Organizer, User]:
    """A verified Organizer with one free admin and one held admin."""
    organizer = verify(OrganizerFactory.create())
    free = _account()
    OrganizerMemberFactory.create(
        organizer=organizer, account=free, role=MemberRole.ADMIN
    )
    OrganizerMemberFactory.create(
        organizer=organizer,
        account=_account(),
        role=MemberRole.ADMIN,
        hold_ends_at=timezone.now() + timedelta(hours=20),
    )
    return organizer, free


def test_demoting_the_last_free_admin_is_refused() -> None:
    organizer, free = _setup()

    with pytest.raises(self_service.Conflict):
        self_service.change_member_role(
            organizer.pk,
            free.player.public_code,
            actor=free,
            role=MemberRole.MATCH_MANAGER,
        )

    assert OrganizerMember.objects.get(account=free).role == MemberRole.ADMIN


def test_removing_the_last_free_admin_is_refused() -> None:
    organizer, free = _setup()

    with pytest.raises(self_service.Conflict):
        self_service.remove_member(organizer.pk, free.player.public_code, actor=free)

    assert OrganizerMember.objects.filter(account=free).exists()


def test_the_last_free_admin_cannot_remove_totp() -> None:
    _, free = _setup()
    session = HeadlessSession(APP)
    assert (
        session.mfa_login(free.email, totp_code(totp_secret(free))).status_code == 200
    )
    assert session.reauthenticate().status_code == 200

    response = session.delete("account/authenticators/totp")

    assert response.status_code == 409
    assert Authenticator.objects.filter(user=free, type="totp").exists()
