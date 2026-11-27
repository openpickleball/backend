"""Granting an Organizer that is being deleted is NotFound, not a 500 (#230).

``grant_organizer`` resolves the Organizer code and then inserts the
grant. While another connection holds the Organizer row and then
deletes it, the grant must wait for that transaction and answer
``NotFound``, never let the insert fail with an ``IntegrityError``.
Runs in real transactions, with Audit Events switched off (the audit
table refuses the teardown's flush once it has a row). Made-up data
only.
"""

import threading
import time

import pytest
from allauth.account.models import EmailAddress
from django.db import connection, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory, enroll_totp
from apps.audit import services as audit_services
from apps.clubs.club_self_service import grant_organizer
from apps.clubs.models import ClubOrganizerGrant, Organizer
from apps.clubs.self_service import NotFound
from apps.clubs.tests.factories import ClubFactory, ClubRoleFactory, OrganizerFactory
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db(transaction=True)

HOLD_SECONDS = 1.5


def test_a_grant_to_an_organizer_deleted_meanwhile_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audit_services, "record_event", lambda **kwargs: None)
    club = ClubFactory.create()
    admin: User = UserFactory.create()
    EmailAddress.objects.create(user=admin, email=admin.email, verified=True)
    enroll_totp(admin)
    PlayerFactory.create(account=admin)
    ClubRoleFactory.create(club=club, account=admin)
    organizer = OrganizerFactory.create()
    code = organizer.public_code
    locked = threading.Event()

    def delete_organizer() -> None:
        try:
            with transaction.atomic():
                list(Organizer.objects.select_for_update().filter(pk=organizer.pk))
                locked.set()
                time.sleep(HOLD_SECONDS)
                Organizer.objects.filter(pk=organizer.pk).delete()
        finally:
            connection.close()

    other = threading.Thread(target=delete_organizer)
    other.start()
    assert locked.wait(timeout=10)
    try:
        with pytest.raises(NotFound):
            grant_organizer(club.pk, code, actor=admin)
    finally:
        other.join(timeout=30)

    assert not ClubOrganizerGrant.objects.exists()
