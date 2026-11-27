"""A deleted Account's own endpoints answer 401, never 500 (#344, ADR 0048).

Account deletion keeps an Account whose Player is in the Ledger, inactive
and cleared, and keeps the Player with no birth year or month. Nothing
may treat that Account as signed in: ``/me/player`` would fail to
serialize the cleared Player, and the data export would read it. Its
sessions are deleted with it, and a session written for it afterwards
(a login that passed its password check just before the deletion
committed) stays signed out, since the Account is inactive. Made-up data
only.
"""

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.deletion import delete_account
from apps.accounts.tests.factories import DEFAULT_PASSWORD
from apps.accounts.tests.headless import APP, BROWSER, HeadlessSession, csrf_header
from apps.matches.models import Team
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory
from apps.ratings.tests.builders import saved_match

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

ME_PLAYER = "/api/v1/me/player"
EXPORT = "/api/v1/me/export"
DOWNLOAD = "/api/v1/me/export/download"
CALLS = [
    ("get", ME_PLAYER, None),
    ("patch", ME_PLAYER, {"display_name": "Robin Q."}),
    ("post", EXPORT, {}),
    ("post", DOWNLOAD, {"key": "made-up-key"}),
]


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


def _played_player() -> Player:
    """A Player in the Ledger, so deletion keeps it and its Account."""
    player = PlayerFactory.create()
    EmailAddress.objects.create(
        user=player.account, email=player.account.email, verified=True, primary=True
    )
    others = [PlayerFactory.create() for _ in range(3)]
    saved_match(players={Team.A: [player, others[0]], Team.B: others[1:]})
    return player


def _delete(player: Player) -> "_User":
    result = delete_account(player.account, actor=player.account)
    assert result.account_kept
    assert result.player_kept
    stored = Player.objects.get(pk=player.pk)
    assert stored.birth_year is None
    account: _User = get_user_model()._default_manager.get(pk=player.account_id)
    return account


def _call(client: Client, method: str, path: str, data: Any, headers: Any) -> Any:
    if data is None:
        return getattr(client, method)(path, headers=headers)
    return getattr(client, method)(
        path, json.dumps(data), content_type="application/json", headers=headers
    )


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize(("method", "path", "data"), CALLS)
def test_a_session_from_before_the_deletion_is_signed_out(
    base: str, method: str, path: str, data: Any
) -> None:
    player = _played_player()
    session = HeadlessSession(base)
    assert session.login(player.account.email, DEFAULT_PASSWORD).status_code == 200
    assert session.reauthenticate(DEFAULT_PASSWORD).status_code == 200
    before = _call(session.client, "get", ME_PLAYER, None, session._headers())
    assert before.status_code == 200
    _delete(player)

    response = _call(session.client, method, path, data, session._headers())

    assert response.status_code == 401


def _forced_session(account: "_User", *, token: bool) -> tuple[Client, dict[str, str]]:
    """A client and headers for a session written for ``account`` by
    ``force_login``, sent as the cookie or as the app's session token."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(account)
    if token:
        return Client(), {"X-Session-Token": client.session.session_key or ""}
    assert client.get(f"{BROWSER}/config").status_code == 200
    return client, csrf_header(client)


@pytest.mark.parametrize("token", [False, True], ids=["browser", "app"])
def test_such_a_session_signs_in_an_active_account(token: bool) -> None:
    """The control: the same session, for an Account not deleted."""
    caller, headers = _forced_session(_played_player().account, token=token)

    assert _call(caller, "get", ME_PLAYER, None, headers).status_code == 200


@pytest.mark.parametrize("token", [False, True], ids=["browser", "app"])
@pytest.mark.parametrize(("method", "path", "data"), CALLS)
def test_a_session_written_after_the_deletion_is_signed_out(
    token: bool, method: str, path: str, data: Any
) -> None:
    caller, headers = _forced_session(_delete(_played_player()), token=token)

    response = _call(caller, method, path, data, headers)

    assert response.status_code == 401
