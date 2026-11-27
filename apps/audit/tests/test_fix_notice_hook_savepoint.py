"""A database error in a row's notice hook never fails the change
(#232 review).

``apps.audit.access`` catches a failing ``access_notice``. Without a
savepoint, a database error there leaves the transaction aborted, so the
change fails anyway. The hook runs in a savepoint, so the change goes on.
Made-up data only.
"""

from typing import Any

import pytest
from django.db import connection

from apps.accounts.tests.factories import UserFactory
from apps.audit.access import delete_access_row, save_access_row
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole, OrganizerMember
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory

pytestmark = pytest.mark.django_db


def _broken_hook(self: Any, *args: object, **kwargs: object) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 / 0")


@pytest.fixture(autouse=True)
def _broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OrganizerMember, "access_notice", _broken_hook)


def test_a_removal_still_commits_its_work() -> None:
    member = OrganizerMemberFactory.create()

    delete_access_row(member, actor=None)

    assert not OrganizerMember.objects.filter(pk=member.pk).exists()
    assert AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_MEMBER_REMOVE
    ).exists()


def test_an_add_still_commits_its_work() -> None:
    organizer = OrganizerFactory.create()
    account = UserFactory.create()

    save_access_row(
        OrganizerMember(
            organizer=organizer, account=account, role=MemberRole.MATCH_MANAGER
        ),
        actor=None,
    )

    assert OrganizerMember.objects.filter(account=account).exists()
