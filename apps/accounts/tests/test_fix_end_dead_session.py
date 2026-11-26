"""Only a live session can be ended and audited (#100 review).

A user session row whose session is already gone must not be listed as
endable: naming it gets the fixed refusal and records no
``session.end``. Made-up data only.
"""

import pytest
from allauth.account.models import EmailAddress
from django.contrib.sessions.models import Session

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.audit.models import AuditAction, AuditEvent

pytestmark = pytest.mark.django_db

EMAIL = "dead.session@example.org"


@pytest.mark.parametrize("base", [APP, BROWSER])
def test_a_dead_session_cannot_be_ended(base: str) -> None:
    account = UserFactory.create(email=EMAIL, password=PASSWORD)
    EmailAddress.objects.create(user=account, email=EMAIL, primary=True, verified=True)
    current = HeadlessSession(base)
    assert current.login(EMAIL).status_code == 200
    dead = HeadlessSession(APP)
    assert dead.login(EMAIL).status_code == 200
    listed = current.get("auth/sessions").json()["data"]
    dead_id = next(item["id"] for item in listed if not item["is_current"])
    Session.objects.filter(session_key=dead.token).delete()

    response = current.delete_json("auth/sessions", {"sessions": [dead_id]})

    assert response.status_code == 400
    assert not AuditEvent.objects.filter(action=AuditAction.SESSION_END).exists()
