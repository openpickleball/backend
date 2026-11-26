"""Confirm and dispute document their only 403: a session's CSRF failure.

A signed-in browser session that POSTs without its CSRF token gets 403
(criterion 3). A caller who may not change the match gets 404, as for an
unknown code, never 403 (ADR 0008). The OpenAPI document must list the
CSRF 403 and say that it is the only one. ``browser_client`` enforces CSRF
(``Client(enforce_csrf_checks=True)``). Made-up data only.
"""

from collections.abc import Iterator
from datetime import date

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    browser_client,
    csrf_header,
    login,
    signup,
    verify_from_last_mail,
)
from apps.matches.models import Match, MatchFormat
from apps.matches.services import record_match
from apps.players.tests.factories import PlayerFactory

# Queued account email is sent when each request returns (ADR 0016).
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

EMAIL = "csrf.confirmer@example.org"
ERROR_REF = {"$ref": "#/components/schemas/ErrorResponse"}
ACTIONS = ["confirm", "dispute"]


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.parametrize("action", ACTIONS)
def test_operation_documents_the_csrf_403(action: str) -> None:
    document = Client().get("/api/v1/openapi.json").json()
    operation = document["paths"][f"/api/v1/matches/{{code}}/{action}"]["post"]

    forbidden = operation["responses"]["403"]
    assert forbidden["content"]["application/json"]["schema"] == ERROR_REF
    description = forbidden["description"]
    assert "CSRF" in description
    assert "404" in description


def _signed_in_participant() -> tuple[Client, Match]:
    signup(Client(), APP, EMAIL)
    verify_from_last_mail(Client(), APP)
    account = get_user_model().objects.get_by_natural_key(EMAIL)
    mine = PlayerFactory.create(account=account)
    opponent = PlayerFactory.create()
    match = record_match(
        format=MatchFormat.SINGLES,
        team_a=[opponent],
        team_b=[mine],
        games=[(11, 6)],
        played_on=date(2026, 6, 1),
        submitted_by_user=opponent.account,
    ).match
    client = browser_client()
    assert client.cookies[settings.CSRF_COOKIE_NAME]
    assert login(client, BROWSER, EMAIL, **csrf_header(client)).status_code == 200
    return client, match


@pytest.mark.parametrize("action", ACTIONS)
def test_session_post_without_csrf_token_gets_403(action: str) -> None:
    client, match = _signed_in_participant()

    response = client.post(f"/api/v1/matches/{match.public_code}/{action}")

    assert response.status_code == 403
    assert response.json() == {
        "code": "forbidden",
        "message": "CSRF check Failed",
        "errors": [],
    }


@pytest.mark.parametrize("action", ACTIONS)
def test_session_post_with_csrf_token_gets_through(action: str) -> None:
    client, match = _signed_in_participant()

    response = client.post(
        f"/api/v1/matches/{match.public_code}/{action}", headers=csrf_header(client)
    )

    assert response.status_code == 200
