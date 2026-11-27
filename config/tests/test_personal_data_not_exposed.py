"""No API operation returns or documents a Player's encrypted personal
data, except the Account's own Player operations (issue #115, ADR 0009,
ADR 0046).

The legal name, birth year and month, and gender division are returned
only by ``create_my_player``, ``get_my_player``, and ``update_my_player``,
and in the Account's own data export (``download_data_export``, #39).
Every other operation's responses are walked in the committed OpenAPI
document and in the live one, and two operations that show other
players, and allauth's session payload, are called. Made-up data only.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.headless import APP, BROWSER, PASSWORD, HeadlessSession
from apps.players.tests.factories import PlayerFactory
from config.api import api

BASE_DIR = Path(__file__).resolve().parent.parent.parent
PERSONAL = frozenset({"legal_name", "birth_year", "birth_month", "gender_division"})
OWN_PLAYER_OPERATIONS = frozenset(
    {"create_my_player", "get_my_player", "update_my_player", "download_data_export"}
)
LEGAL_NAME = "Orvanthe Made-up Legal"


def _documents() -> list[dict[str, Any]]:
    committed = json.loads((BASE_DIR / "openapi" / "openapi.json").read_text())
    return [committed, api.get_openapi_schema()]


def _resolve(document: dict[str, Any], ref: str) -> Any:
    node: Any = document
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


def _keys(document: dict[str, Any], node: Any, seen: set[str]) -> Iterator[str]:
    """Every key and string reachable from ``node``, following $refs."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                if value not in seen:
                    seen.add(value)
                    yield from _keys(document, _resolve(document, value), seen)
                continue
            yield key
            yield from _keys(document, value, seen)
    elif isinstance(node, list):
        for item in node:
            yield from _keys(document, item, seen)
    elif isinstance(node, str):
        yield node


def _operations(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    for operations in document["paths"].values():
        for operation in operations.values():
            yield operation["operationId"], operation


def _found(value: Any) -> set[str]:
    """The personal data keys anywhere in a JSON value."""
    text = json.dumps(value)
    return {name for name in PERSONAL if f'"{name}"' in text}


@pytest.mark.parametrize("index", [0, 1], ids=["committed", "live"])
def test_no_other_operation_documents_personal_data(index: int) -> None:
    document = _documents()[index]
    checked = set()
    for operation_id, operation in _operations(document):
        if operation_id in OWN_PLAYER_OPERATIONS:
            continue
        reachable = set(_keys(document, operation.get("responses", {}), set()))
        assert not PERSONAL & reachable, operation_id
        checked.add(operation_id)
    assert {"get_player", "search_players", "get_player_ratings"} <= checked


@pytest.mark.parametrize("index", [0, 1], ids=["committed", "live"])
def test_the_own_player_operations_are_the_only_ones(index: int) -> None:
    # Guards the test above: it must skip exactly these operations.
    document = _documents()[index]
    holders = {
        operation_id
        for operation_id, operation in _operations(document)
        if PERSONAL & set(_keys(document, operation.get("responses", {}), set()))
    }
    assert holders == OWN_PLAYER_OPERATIONS


@pytest.fixture
def viewer_and_player() -> tuple[Client, str]:
    cache.clear()
    player = PlayerFactory.create(
        legal_name=LEGAL_NAME, display_name="Orvanthe M.", gender_division="womens"
    )
    client = Client()
    client.force_login(UserFactory.create())
    return client, player.public_code


@pytest.mark.django_db
def test_a_profile_returns_no_personal_data(
    viewer_and_player: tuple[Client, str],
) -> None:
    client, code = viewer_and_player

    response = client.get(f"/api/v1/players/{code}")

    assert response.status_code == 200
    assert not _found(response.json())
    assert LEGAL_NAME not in response.content.decode()


@pytest.mark.django_db
def test_search_returns_no_personal_data(
    viewer_and_player: tuple[Client, str],
) -> None:
    client, code = viewer_and_player

    response = client.get("/api/v1/players/search", {"q": "Orvanthe"})

    assert response.status_code == 200
    assert code in response.content.decode()
    assert not _found(response.json())
    assert LEGAL_NAME not in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", [BROWSER, APP], ids=["browser", "app"])
def test_allauth_session_payload_has_no_personal_data(kind: str) -> None:
    cache.clear()
    account = UserFactory.create(password=PASSWORD)
    EmailAddress.objects.create(
        user=account, email=account.email, verified=True, primary=True
    )
    PlayerFactory.create(account=account, legal_name=LEGAL_NAME)
    session = HeadlessSession(kind)

    login = session.login(account.email)
    current = session.get("auth/session")

    assert current.status_code == 200
    for response in (login, current):
        assert not _found(response.json())
        assert LEGAL_NAME not in response.content.decode()
