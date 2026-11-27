"""An under-13 removal of an Account with no Player names no Player field
(#190, ADR 0059). Made-up data only."""

import pytest

from apps.accounts.deletion import delete_account
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db


def test_the_event_lists_no_fields_when_the_account_has_no_player() -> None:
    account = UserFactory.create()
    moderator = UserFactory.create()

    delete_account(account, actor=moderator, action=AuditAction.PLAYER_UNDERAGE_REMOVE)

    event = AuditEvent.objects.get(action=AuditAction.PLAYER_UNDERAGE_REMOVE)
    assert event.after == {"account_kept": False, "player_kept": None}
