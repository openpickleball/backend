"""A password set, not changed, is recorded as a set (#100 review).

allauth's change endpoint sets a first password for an Account with no
usable one (no current password sent). Its ``password.change`` event
records ``{"method": "set"}``. Made-up data only.
"""

import time

import pytest
from allauth.account.authentication import AUTHENTICATION_METHODS_SESSION_KEY
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import BROWSER, NEW_PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db


def test_setting_a_first_password_records_set() -> None:
    account = UserFactory.create(email="set.password@example.org")
    account.set_unusable_password()
    account.save()
    EmailAddress.objects.create(
        user=account, email=account.email, primary=True, verified=True
    )
    session = HeadlessSession(BROWSER)
    session.client.force_login(account)
    store = session.client.session
    # A recent authentication, so allauth asks for no reauthentication.
    store[AUTHENTICATION_METHODS_SESSION_KEY] = [
        {"method": "password", "at": time.time(), "email": account.email}
    ]
    store.save()

    response = session.post("account/password/change", {"new_password": NEW_PASSWORD})

    assert response.status_code == 200, response.content
    event = AuditEvent.objects.get(action=AuditAction.PASSWORD_CHANGE)
    assert event.after == {"method": "set"}
