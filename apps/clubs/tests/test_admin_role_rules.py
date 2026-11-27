"""An admin role needs an active Account with a verified login address
(#232, ADR 0057).

``OrganizerMember.clean()`` and ``ClubRole.clean()`` refuse an admin
role for an inactive Account or one whose login address is not
verified, with fixed text on ``account``. A match manager role is not
checked. ``change_member_role`` refuses a promotion to admin by the same
rule with ``Conflict``. Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.clubs import self_service
from apps.clubs.models import (
    ADMIN_ACCOUNT_MESSAGE,
    ClubRole,
    ClubRoleKind,
    MemberRole,
    OrganizerMember,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
)
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _account(*, verified: bool = True, active: bool = True) -> User:
    account: User = UserFactory.create(is_active=active)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=verified, primary=True
    )
    return account


def _messages(error: ValidationError) -> dict[str, list[str]]:
    return error.message_dict


@pytest.mark.parametrize(
    ("verified", "active"), [(False, True), (True, False), (False, False)]
)
def test_an_organizer_admin_needs_an_active_account_with_a_verified_address(
    verified: bool, active: bool
) -> None:
    member = OrganizerMember(
        organizer=OrganizerFactory.create(),
        account=_account(verified=verified, active=active),
        role=MemberRole.ADMIN,
    )

    with pytest.raises(ValidationError) as caught:
        member.full_clean()

    assert _messages(caught.value) == {"account": [ADMIN_ACCOUNT_MESSAGE]}


def test_an_account_with_no_address_row_cannot_be_an_organizer_admin() -> None:
    member = OrganizerMember(
        organizer=OrganizerFactory.create(),
        account=UserFactory.create(),
        role=MemberRole.ADMIN,
    )

    with pytest.raises(ValidationError) as caught:
        member.full_clean()

    assert _messages(caught.value) == {"account": [ADMIN_ACCOUNT_MESSAGE]}


def test_a_verified_active_account_may_be_an_organizer_admin() -> None:
    member = OrganizerMember(
        organizer=OrganizerFactory.create(), account=_account(), role=MemberRole.ADMIN
    )

    member.full_clean()


def test_a_match_manager_role_is_not_checked() -> None:
    member = OrganizerMember(
        organizer=OrganizerFactory.create(),
        account=_account(verified=False),
        role=MemberRole.MATCH_MANAGER,
    )

    member.full_clean()


@pytest.mark.parametrize(
    ("verified", "active"), [(False, True), (True, False), (False, False)]
)
def test_a_club_admin_needs_an_active_account_with_a_verified_address(
    verified: bool, active: bool
) -> None:
    role = ClubRole(
        club=ClubFactory.create(),
        account=_account(verified=verified, active=active),
        role=ClubRoleKind.ADMIN,
    )

    with pytest.raises(ValidationError) as caught:
        role.full_clean()

    assert _messages(caught.value) == {"account": [ADMIN_ACCOUNT_MESSAGE]}


def test_a_verified_active_account_may_be_a_club_admin() -> None:
    role = ClubRole(
        club=ClubFactory.create(), account=_account(), role=ClubRoleKind.ADMIN
    )

    role.full_clean()


def test_a_promotion_to_admin_refuses_an_unverified_address() -> None:
    organizer = OrganizerFactory.create()
    admin = _account()
    enroll_totp(admin)
    OrganizerMemberFactory.create(
        organizer=organizer, account=admin, role=MemberRole.ADMIN
    )
    manager = _account(verified=False)
    PlayerFactory.create(account=manager)
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    with pytest.raises(self_service.Conflict) as caught:
        self_service.change_member_role(
            organizer.pk,
            manager.player.public_code,
            actor=admin,
            role=MemberRole.ADMIN,
        )

    assert str(caught.value) == self_service.ADMIN_ACCOUNT_CONFLICT_MESSAGE
    assert (
        OrganizerMember.objects.get(organizer=organizer, account=manager).role
        == MemberRole.MATCH_MANAGER
    )


def test_a_promotion_to_admin_of_a_verified_member_works() -> None:
    organizer = OrganizerFactory.create()
    admin = _account()
    enroll_totp(admin)
    OrganizerMemberFactory.create(
        organizer=organizer, account=admin, role=MemberRole.ADMIN
    )
    manager = _account()
    PlayerFactory.create(account=manager)
    OrganizerMemberFactory.create(organizer=organizer, account=manager)

    changed = self_service.change_member_role(
        organizer.pk, manager.player.public_code, actor=admin, role=MemberRole.ADMIN
    )

    assert changed.role == MemberRole.ADMIN
