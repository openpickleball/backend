"""The API refuses an Account that needs MFA and has none (issue #99,
ADR 0043): 403 ``mfa_required`` on every signed-in operation, from both
clients, while allauth's endpoints stay open so it can enroll."""

from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.test import Client

from apps.accounts.tests.factories import UserFactory, enroll_totp, totp_code
from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    PASSWORD,
    HeadlessSession,
    browser_client,
    csrf_header,
)
from apps.matches.permissions import MODERATOR_GROUP
from apps.partners.tests.factories import PartnerApplicationFactory
from config.auth import MFA_REQUIRED_MESSAGE

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = pytest.mark.django_db

ME = "/api/v1/accounts/me"
ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}


@pytest.fixture(autouse=True)
def _one_step_of_clock_drift(settings: Any) -> None:
    settings.MFA_TOTP_TOLERANCE = 1


def _verified(**fields: Any) -> "_User":
    account = UserFactory.create(password=PASSWORD, **fields)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    return account


def _staff() -> "_User":
    return _verified(is_staff=True, mfa=False)


def _superuser() -> "_User":
    return _verified(is_superuser=True, mfa=False)


def _moderator() -> "_User":
    account = _verified()
    account.groups.add(Group.objects.get(name=MODERATOR_GROUP))
    return account


def _production_owner() -> "_User":
    account = _verified()
    PartnerApplicationFactory.create(user=account, is_sandbox=False)
    return account


NEEDS_MFA = [_staff, _superuser, _moderator, _production_owner]
NEEDS_MFA_IDS = ["staff", "superuser", "moderator", "production_owner"]


def _get(session: HeadlessSession, path: str) -> Any:
    if session.base == BROWSER:
        return session.client.get(path)
    return session.client.get(path, headers={"X-Session-Token": session.token or ""})


@pytest.fixture(params=[BROWSER, APP], ids=["browser", "app"])
def base(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _assert_mfa_required(response: Any) -> None:
    assert response.status_code == 403
    assert response.json() == {
        "code": "mfa_required",
        "message": MFA_REQUIRED_MESSAGE,
        "errors": [],
    }


@pytest.mark.parametrize("make", NEEDS_MFA, ids=NEEDS_MFA_IDS)
def test_an_account_that_needs_mfa_is_refused_without_it(base: str, make: Any) -> None:
    account = make()
    session = HeadlessSession(base)
    assert session.login(account.email).status_code == 200

    _assert_mfa_required(_get(session, ME))


@pytest.mark.parametrize("make", NEEDS_MFA, ids=NEEDS_MFA_IDS)
def test_the_same_operation_succeeds_after_enrolling(base: str, make: Any) -> None:
    account = make()
    session = HeadlessSession(base)
    session.login(account.email)
    session.reauthenticate()
    secret = session.get("account/authenticators/totp").json()["meta"]["secret"]

    enrolled = session.post("account/authenticators/totp", {"code": totp_code(secret)})

    assert enrolled.status_code == 200
    response = _get(session, ME)
    assert response.status_code == 200
    assert response.json()["email"] == account.email


def test_a_player_without_mfa_is_not_refused(base: str) -> None:
    account = _verified()
    session = HeadlessSession(base)
    session.login(account.email)

    assert _get(session, ME).status_code == 200


def test_an_enrolled_staff_account_is_let_through(base: str) -> None:
    account = _staff()
    secret = enroll_totp(account).secret
    session = HeadlessSession(base)

    session.mfa_login(account.email, totp_code(secret))

    assert _get(session, ME).status_code == 200


def test_no_account_still_gets_401() -> None:
    response = Client().get(ME)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_browser_session_without_csrf_still_gets_the_csrf_403() -> None:
    account = _staff()
    client = browser_client()
    client.post(
        f"{BROWSER}/auth/login",
        {"email": account.email, "password": PASSWORD},
        content_type="application/json",
        headers=csrf_header(client),
    )

    response = client.post("/api/v1/me/player", {}, content_type="application/json")

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_browser_session_with_csrf_gets_mfa_required() -> None:
    account = _staff()
    client = browser_client()
    client.post(
        f"{BROWSER}/auth/login",
        {"email": account.email, "password": PASSWORD},
        content_type="application/json",
        headers=csrf_header(client),
    )

    response = client.post(
        "/api/v1/me/player",
        {},
        content_type="application/json",
        headers=csrf_header(client),
    )

    _assert_mfa_required(response)


def test_every_signed_in_operation_documents_the_403() -> None:
    document = Client().get("/api/v1/openapi.json").json()

    operations = [
        operation
        for path_item in document["paths"].values()
        for operation in path_item.values()
        if operation.get("security")
    ]
    assert operations
    for operation in operations:
        forbidden = operation["responses"]["403"]
        assert forbidden["content"]["application/json"]["schema"] == ERROR_REF
        assert "mfa_required" in forbidden["description"]


def test_an_operations_own_403_description_is_kept() -> None:
    document = Client().get("/api/v1/openapi.json").json()

    forbidden = document["paths"]["/api/v1/matches/{code}/confirm"]["post"][
        "responses"
    ]["403"]

    assert "CSRF" in forbidden["description"]
    assert "mfa_required" in forbidden["description"]
