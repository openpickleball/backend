"""POST /api/v1/organizers/{code}/reports (#271).

A signed-in Account with a Player reports a verified Organizer with one
of the fixed reasons. Every refusal gets the same 404 as an unknown code;
a second open report gets 409; the operation has its own day throttle.
The response never names the reporter or holds a row id. Made-up data
only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.api import OrganizerReportThrottle
from apps.clubs.models import Organizer, OrganizerReport
from apps.clubs.reports import close_report
from apps.clubs.tests.factories import OrganizerFactory, verified_organizer
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from config.throttling import FixedWindowThrottle

pytestmark = pytest.mark.django_db

NOW = 1_800_000_000.0
REASON = "Made-up moderator reason."
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}


@pytest.fixture(autouse=True)
def _pinned_throttles(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cache.clear()
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: NOW))
    yield
    cache.clear()


def _url(code: str) -> str:
    return f"/api/v1/organizers/{code}/reports"


def _client(account: User) -> Client:
    client = Client()
    client.force_login(account)
    return client


def _reporter() -> User:
    account: User = PlayerFactory.create().account
    return account


def _post(client: Client, code: str, body: Any = None) -> Any:
    return client.post(
        _url(code),
        {"reason": "wrong_results"} if body is None else body,
        content_type="application/json",
    )


def test_a_report_returns_201_with_code_reason_and_time() -> None:
    organizer = verified_organizer()
    account = _reporter()

    response = _post(_client(account), organizer.public_code)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"organizer", "reason", "created_at"}
    assert body["organizer"] == organizer.public_code
    assert body["reason"] == "wrong_results"
    report = OrganizerReport.objects.get()
    assert report.reporter_id == account.pk
    assert account.email not in response.content.decode()
    assert str(account.pk) not in body.values()
    assert AuditEvent.objects.filter(
        action=AuditAction.ORGANIZER_REPORT_CREATE, actor_user=account
    ).exists()


def test_the_code_may_be_lowercase_without_hyphens() -> None:
    organizer = verified_organizer()

    response = _post(
        _client(_reporter()), organizer.public_code.replace("-", "").lower()
    )

    assert response.status_code == 201


def test_a_second_open_report_is_409() -> None:
    organizer = verified_organizer()
    client = _client(_reporter())
    _post(client, organizer.public_code)

    response = _post(client, organizer.public_code)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert OrganizerReport.objects.count() == 1


def test_after_a_close_a_new_report_is_accepted() -> None:
    organizer = verified_organizer()
    client = _client(_reporter())
    _post(client, organizer.public_code)
    moderator: User = UserFactory.create()
    moderator.user_permissions.add(Permission.objects.get(codename="verify_organizer"))
    close_report(OrganizerReport.objects.get(), actor=moderator, reason=REASON)

    assert _post(client, organizer.public_code).status_code == 201


def _unknown_code() -> str:
    return generate(CodeType.ORGANIZER)


def _refusals() -> list[tuple[Client, str]]:
    verified = verified_organizer()
    inactive = _reporter()
    client_inactive = _client(inactive)
    inactive.is_active = False
    inactive.save()
    return [
        (_client(_reporter()), OrganizerFactory.create().public_code),
        (_client(UserFactory.create()), verified.public_code),
        (_client(_reporter()), "not-a-code"),
        (client_inactive, verified.public_code),
    ]


def test_every_refusal_is_the_unknown_code_404() -> None:
    unknown = _post(_client(_reporter()), _unknown_code())
    assert unknown.status_code == 404
    assert unknown.json() == NOT_FOUND

    for client, code in _refusals()[:3]:
        response = _post(client, code)
        assert response.status_code == 404
        assert response.content == unknown.content
    assert not OrganizerReport.objects.exists()


def test_an_inactive_account_is_not_signed_in() -> None:
    client, code = _refusals()[3]

    response = _post(client, code)

    assert response.status_code == 401
    assert not OrganizerReport.objects.exists()


def test_signed_out_is_401() -> None:
    response = _post(Client(), verified_organizer().public_code)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"reason": "made-up-MARKER"},
        {"reason": ""},
        {"reason": None},
        {},
        {"reason": "other", "note": "free text MARKER"},
    ],
)
def test_a_bad_reason_is_422_whether_or_not_the_organizer_exists(
    body: dict[str, Any],
) -> None:
    client = _client(_reporter())
    known = _post(client, verified_organizer().public_code, body)
    unknown = _post(client, _unknown_code(), body)

    assert known.status_code == unknown.status_code == 422
    assert known.content == unknown.content
    assert "MARKER" not in known.content.decode()
    assert not OrganizerReport.objects.exists()


def test_the_sixth_report_in_a_day_is_429() -> None:
    client = _client(_reporter())
    organizers = [verified_organizer() for _ in range(6)]

    statuses = [_post(client, o.public_code).status_code for o in organizers[:5]]
    refused = _post(client, organizers[5].public_code)

    assert statuses == [201] * 5
    assert refused.status_code == 429
    assert refused.json()["code"] == "throttled"
    assert 1 <= int(refused["Retry-After"]) <= 86_400
    assert OrganizerReport.objects.count() == 5


@override_settings(API_ORGANIZER_REPORT_THROTTLE_PER_DAY=1)
def test_the_day_limit_comes_from_settings() -> None:
    client = _client(_reporter())

    assert _post(client, verified_organizer().public_code).status_code == 201
    assert _post(client, verified_organizer().public_code).status_code == 429


@override_settings(API_ORGANIZER_REPORT_THROTTLE_PER_DAY=1)
def test_accounts_have_separate_report_budgets() -> None:
    organizer = verified_organizer()
    assert _post(_client(_reporter()), organizer.public_code).status_code == 201

    assert _post(_client(_reporter()), organizer.public_code).status_code == 201


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_reports_do_not_use_the_account_budget() -> None:
    client = _client(_reporter())
    for _ in range(3):
        assert _post(client, verified_organizer().public_code).status_code == 201

    assert client.get("/api/v1/accounts/me").status_code == 200


def test_the_throttle_counts_by_account_over_a_day() -> None:
    throttle = OrganizerReportThrottle("organizer_report")

    (window,) = throttle.limits()

    assert window == ("day", 5, 86_400)


def test_organizer_unchanged_by_a_report() -> None:
    organizer = verified_organizer()
    before = list(Organizer.objects.filter(pk=organizer.pk).values())

    _post(_client(_reporter()), organizer.public_code)

    assert list(Organizer.objects.filter(pk=organizer.pk).values()) == before
