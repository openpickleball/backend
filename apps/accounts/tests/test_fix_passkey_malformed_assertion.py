"""A malformed passkey assertion from an allowed origin gets allauth's
400 ``incorrect_code`` at the MFA login stage and at reauthentication,
never a server error (issue #117). Made-up data only."""

from collections.abc import Callable
from typing import Any

import pytest

from apps.accounts.tests.factories import enroll_totp, totp_code
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession
from apps.accounts.tests.passkey_flows import (
    AUTHENTICATE_PATH,
    LOGIN_PATH,
    REAUTHENTICATE_PATH,
    added,
    error_codes,
    new_passkey,
    request_options,
    use_passkey_settings,
    verified_account,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _passkey_settings(settings: Any) -> None:
    use_passkey_settings(settings)


def _without_raw_id(credential: dict[str, Any]) -> None:
    del credential["rawId"]


def _extensions(value: object) -> Callable[[dict[str, Any]], None]:
    def change(credential: dict[str, Any]) -> None:
        credential["clientExtensionResults"] = value

    return change


BREAKS = [_without_raw_id, _extensions(5), _extensions("x")]
BREAK_IDS = ["no-raw-id", "extensions-number", "extensions-text"]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("brk", BREAKS, ids=BREAK_IDS)
def test_reauthentication_refuses_a_malformed_assertion(
    base: str, brk: Callable[[dict[str, Any]], None]
) -> None:
    account = verified_account()
    passkey = new_passkey()
    added(BROWSER, account, passkey)
    session = HeadlessSession(base)
    login = passkey.sign_in(request_options(session, LOGIN_PATH))
    assert session.post(LOGIN_PATH, {"credential": login}).status_code == 200

    credential = passkey.sign_in(request_options(session, REAUTHENTICATE_PATH))
    brk(credential)
    response = session.post(REAUTHENTICATE_PATH, {"credential": credential})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]


@pytest.mark.parametrize("base", [BROWSER, APP], ids=["browser", "app"])
@pytest.mark.parametrize("brk", BREAKS, ids=BREAK_IDS)
def test_the_mfa_stage_refuses_a_malformed_assertion(
    base: str, brk: Callable[[dict[str, Any]], None]
) -> None:
    account = verified_account()
    enrollment = enroll_totp(account)
    passkey = new_passkey()
    adder = HeadlessSession(BROWSER)
    adder.mfa_login(account.email, totp_code(enrollment.secret))
    adder.reauthenticate()
    options = adder.get("account/authenticators/webauthn?passwordless").json()
    credential = passkey.register(options["data"]["creation_options"])
    assert (
        adder.post(
            "account/authenticators/webauthn",
            {"name": "Phone", "credential": credential, "passwordless": True},
        ).status_code
        == 200
    )
    session = HeadlessSession(base)
    session.login(account.email)

    assertion = passkey.sign_in(request_options(session, AUTHENTICATE_PATH))
    brk(assertion)
    response = session.post(AUTHENTICATE_PATH, {"credential": assertion})

    assert response.status_code == 400
    assert error_codes(response) == ["incorrect_code"]
    assert session.get("auth/session").status_code == 401
