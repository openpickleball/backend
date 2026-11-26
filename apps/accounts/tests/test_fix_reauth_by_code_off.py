"""Reauthentication is by password or MFA only, never by an emailed code.

allauth's default reauthentication methods include ``code``, which mails
allauth's own ``login_code`` template (with the request's host in it,
against OPEN's email rules), and its code would satisfy an MFA change.
Made-up data only.
"""

import pytest
from allauth.account import app_settings as account_settings
from allauth.account.models import EmailAddress

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.emails.models import QueuedEmail

pytestmark = pytest.mark.django_db


def test_reauthentication_methods_are_password_and_mfa() -> None:
    assert frozenset({"password", "mfa"}) == account_settings.REAUTHENTICATION_METHODS


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
def test_the_code_reauthentication_endpoints_refuse_and_mail_nothing(
    base: str,
) -> None:
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    session = HeadlessSession(base)
    assert session.login(account.email).status_code == 200

    request = session.post("auth/reauthenticate/code/request", {})
    confirm = session.post("auth/reauthenticate/code/confirm", {"code": "ABC123"})
    refused = session.post("account/authenticators/totp", {"code": "000000"})

    assert request.status_code in (403, 404)
    assert confirm.status_code in (403, 404)
    assert QueuedEmail.objects.count() == 0
    flow_ids = {flow["id"] for flow in refused.json()["data"]["flows"]}
    assert "reauthenticate_by_code" not in flow_ids
