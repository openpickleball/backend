"""Concurrent changes cannot leave an Organizer without an admin (#229).

Two admins remain, and each tries at the same moment, on its own
database connection, to demote or remove itself, or the other. Each
change locks the Organizer row before checking the actor and counting
admins, so they run in turn: exactly one succeeds, and one admin
remains. The other gets ``Conflict`` when it would leave no admin, or
``NotFound`` when the first change already took its admin role.
Runs in real transactions, with Audit Events switched off (the audit
table refuses the teardown's flush once it has a row). Made-up data
only.
"""

import threading
from collections.abc import Callable

import pytest
from allauth.account.models import EmailAddress
from django.db import connection

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit import services as audit_services
from apps.audit.models import AuditEvent
from apps.clubs.models import MemberRole, Organizer, OrganizerMember
from apps.clubs.self_service import (
    Conflict,
    NotFound,
    change_member_role,
    remove_member,
)
from apps.clubs.tests.factories import OrganizerFactory, OrganizerMemberFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db(transaction=True)


def _admin(organizer: Organizer) -> User:
    account: User = UserFactory.create()
    EmailAddress.objects.create(user=account, email=account.email, verified=True)
    enroll_totp(account)
    PlayerFactory.create(account=account)
    OrganizerMemberFactory.create(
        organizer=organizer, account=account, role=MemberRole.ADMIN
    )
    return account


def _demote(organizer: Organizer, actor: User, target: User) -> None:
    change_member_role(
        organizer.pk, target.player.public_code, actor=actor, role="match_manager"
    )


def _remove(organizer: Organizer, actor: User, target: User) -> None:
    remove_member(organizer.pk, target.player.public_code, actor=actor)


def _race(calls: list[Callable[[], None]]) -> list[str]:
    barrier = threading.Barrier(len(calls))
    results: list[str] = []
    lock = threading.Lock()

    def run(call: Callable[[], None]) -> None:
        try:
            barrier.wait()
            call()
            outcome = "ok"
        except Conflict:
            outcome = "conflict"
        except NotFound:
            outcome = "not_found"
        finally:
            connection.close()
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(call,)) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return sorted(results)


@pytest.mark.parametrize("change", [_demote, _remove])
@pytest.mark.parametrize("target", ["self", "other"])
def test_exactly_one_of_two_concurrent_changes_succeeds(
    monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Organizer, User, User], None],
    target: str,
) -> None:
    monkeypatch.setattr(audit_services, "record_event", lambda **kwargs: None)
    organizer = OrganizerFactory.create()
    a, b = _admin(organizer), _admin(organizer)
    a_target, b_target = (a, b) if target == "self" else (b, a)

    results = _race(
        [
            lambda: change(organizer, a, a_target),
            lambda: change(organizer, b, b_target),
        ]
    )

    refused = "conflict" if target == "self" else "not_found"
    assert results == sorted([refused, "ok"])
    admins = OrganizerMember.objects.filter(
        organizer=organizer, role=MemberRole.ADMIN, account__is_active=True
    ).count()
    assert admins == 1
    assert not AuditEvent.objects.exists()
