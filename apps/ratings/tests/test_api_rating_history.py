"""A player's rating history at /api/v1/players/{code}/rating-history (issue #53).

Sign-in is required. The player's own Account always sees it. Another
Account sees it only while the player's ``rating_history_visibility`` is
``signed_in`` and the player's Account is active
(``PlayerPrivacy.visible_to``), as with match history. A viewer it is
hidden from gets the same 404 as an unknown or malformed code. Pages are
limit and offset, newest first, every rating format, with each item's
reliability and the demonstration label, and never a match code, replay
id, delta, or row id. Made-up data only.
"""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import RatingFormat
from apps.matches.tests.factories import MatchFactory
from apps.players.models import Player, Visibility
from apps.players.services import update_privacy
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from apps.ratings.models import RatingChangeKind, RatingHistory
from apps.ratings.tests.factories import make_history, make_replay

pytestmark = pytest.mark.django_db

NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
MAX_OFFSET = 2**63 - 1
START = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ITEM_KEYS = {
    "rating_format",
    "kind",
    "rating",
    "reliability",
    "imported_reliability",
    "provisional",
    "established",
    "spec_version",
    "demonstration",
    "recorded_at",
}


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    # The per-Account throttle's counters live in the cache.
    cache.clear()
    yield
    cache.clear()


def history(client: Client, code: str, **params: Any) -> Any:
    return client.get(f"/api/v1/players/{code}/rating-history", params)


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def encoded(value: object) -> object:
    """``value`` as the API's JSON encoder writes it."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def item_json(row: RatingHistory) -> dict[str, Any]:
    return {
        "rating_format": row.rating_format,
        "kind": row.kind,
        "rating": row.rating,
        "reliability": row.reliability,
        "imported_reliability": row.imported_reliability,
        "provisional": row.provisional,
        "established": row.established,
        "spec_version": row.spec_version,
        "demonstration": True,
        "recorded_at": encoded(row.created_at),
    }


def set_visibility(player: Player, level: str) -> None:
    update_privacy(
        player=player,
        actor=player.account,
        changes={"rating_history_visibility": level},
    )


def deactivate(player: Player) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])


@pytest.fixture
def player() -> Player:
    return PlayerFactory.create()


@pytest.fixture
def rows(player: Player) -> list[RatingHistory]:
    """A live update, a replay, and a removal, oldest first."""
    match = MatchFactory.create()
    live = make_history(
        player,
        kind=RatingChangeKind.LIVE_UPDATE,
        match=match,
        delta=0.125,
        created_at=START,
    )
    replay = make_history(
        player,
        rating_format=RatingFormat.SINGLES,
        rating=4.123456789,
        reliability=72.25,
        imported_reliability=55.5,
        established=True,
        calculated_as_of=date(2026, 3, 31),
        created_at=START + timedelta(days=1),
    )
    removed = make_history(
        player,
        kind=RatingChangeKind.REMOVED,
        replay=make_replay(),
        rating_format=RatingFormat.MIXED_DOUBLES,
        created_at=START + timedelta(days=2),
    )
    return [live, replay, removed]


# Who may see it


def test_a_signed_out_request_gets_401(player: Player, rows: list[Any]) -> None:
    response = history(Client(), player.public_code)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_the_player_sees_every_change_newest_first(
    player: Player, rows: list[RatingHistory]
) -> None:
    response = history(signed_in(player.account), player.public_code)

    assert response.status_code == 200
    assert response.json() == {
        "items": [item_json(row) for row in reversed(rows)],
        "count": 3,
    }


def test_the_player_sees_it_even_when_private(
    player: Player, rows: list[RatingHistory]
) -> None:
    set_visibility(player, Visibility.PRIVATE)

    response = history(signed_in(player.account), player.public_code)

    assert response.status_code == 200
    assert response.json()["count"] == 3


def test_another_account_sees_it_at_signed_in(
    player: Player, rows: list[RatingHistory]
) -> None:
    set_visibility(player, Visibility.SIGNED_IN)

    response = history(signed_in(UserFactory.create()), player.public_code)

    assert response.status_code == 200
    assert response.json()["items"] == [item_json(row) for row in reversed(rows)]


def test_hidden_unknown_and_malformed_codes_get_the_same_404(
    player: Player, rows: list[RatingHistory]
) -> None:
    viewer = signed_in(UserFactory.create())
    private = PlayerFactory.create()
    make_history(private)
    set_visibility(private, Visibility.PRIVATE)
    inactive = PlayerFactory.create()
    make_history(inactive)
    set_visibility(inactive, Visibility.SIGNED_IN)
    deactivate(inactive)
    minor = PlayerFactory.create(minor=True)
    make_history(minor)

    unknown = history(viewer, generate(CodeType.PLAYER))
    answers = [
        history(viewer, private.public_code),
        history(viewer, inactive.public_code),
        history(viewer, minor.public_code),
        history(viewer, "not-a-code"),
        history(viewer, generate(CodeType.MATCH)),
    ]

    assert unknown.status_code == 404
    assert unknown.json() == NOT_FOUND
    for response in answers:
        assert response.status_code == 404
        assert response.json() == unknown.json()


def test_a_minor_sees_their_own_history() -> None:
    minor = PlayerFactory.create(minor=True)
    row = make_history(minor)

    response = history(signed_in(minor.account), minor.public_code)

    assert response.status_code == 200
    assert response.json()["items"] == [item_json(row)]


def test_a_minor_who_opens_it_is_seen_by_another_account() -> None:
    minor = PlayerFactory.create(minor=True)
    row = make_history(minor)
    set_visibility(minor, Visibility.SIGNED_IN)

    response = history(signed_in(UserFactory.create()), minor.public_code)

    assert response.status_code == 200
    assert response.json()["items"] == [item_json(row)]


# What an item holds


def test_items_hold_no_match_replay_delta_or_row_id(
    player: Player, rows: list[RatingHistory]
) -> None:
    items = history(signed_in(player.account), player.public_code).json()["items"]

    for item in items:
        assert set(item) == ITEM_KEYS
        assert item["demonstration"] is True
    assert {item["kind"] for item in items} == {
        "live_update",
        "replay",
        "removed",
    }
    text = json.dumps(items)
    assert rows[0].match is not None
    assert rows[0].match.public_code not in text
    assert player.public_code not in text


def test_values_are_returned_as_stored(
    player: Player, rows: list[RatingHistory]
) -> None:
    items = history(signed_in(player.account), player.public_code).json()["items"]

    singles = next(item for item in items if item["rating_format"] == "singles")
    assert singles["rating"] == 4.123456789
    assert singles["reliability"] == 72.25
    assert singles["imported_reliability"] == 55.5
    assert singles["established"] is True
    assert singles["provisional"] is False


def test_another_players_history_is_not_included(
    player: Player, rows: list[RatingHistory]
) -> None:
    make_history(PlayerFactory.create())

    response = history(signed_in(player.account), player.public_code)

    assert response.json()["count"] == 3


def test_a_player_with_no_history_gets_an_empty_page(player: Player) -> None:
    response = history(signed_in(player.account), player.public_code)

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_rows_recorded_at_the_same_time_are_newest_id_first(player: Player) -> None:
    first = make_history(player, created_at=START)
    second = make_history(player, created_at=START)

    items = history(signed_in(player.account), player.public_code).json()["items"]

    assert items == [item_json(second), item_json(first)]


# Paging


def test_the_default_page_is_20(player: Player) -> None:
    for day in range(25):
        make_history(player, created_at=START + timedelta(days=day))

    body = history(signed_in(player.account), player.public_code).json()

    assert body["count"] == 25
    assert len(body["items"]) == 20


def test_limit_and_offset_page_through_newest_first(
    player: Player, rows: list[RatingHistory]
) -> None:
    body = history(
        signed_in(player.account), player.public_code, limit=1, offset=1
    ).json()

    assert body == {"items": [item_json(rows[1])], "count": 3}


def test_the_largest_limit_is_100(player: Player) -> None:
    for day in range(101):
        make_history(player, created_at=START + timedelta(days=day))
    client = signed_in(player.account)

    body = history(client, player.public_code, limit=100).json()

    assert len(body["items"]) == 100
    assert body["count"] == 101


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"offset": MAX_OFFSET + 1}, "query.offset"),
    ],
)
def test_out_of_range_paging_gets_422(
    player: Player, params: dict[str, int], field: str
) -> None:
    response = history(signed_in(player.account), player.public_code, **params)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert [error["field"] for error in body["errors"]] == [field]


def test_largest_offset_returns_an_empty_page(player: Player) -> None:
    response = history(signed_in(player.account), player.public_code, offset=MAX_OFFSET)

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_a_bad_limit_gets_the_same_422_for_a_hidden_and_an_unknown_code() -> None:
    viewer = signed_in(UserFactory.create())
    private = PlayerFactory.create()
    set_visibility(private, Visibility.PRIVATE)

    hidden = history(viewer, private.public_code, limit=0)
    unknown = history(viewer, generate(CodeType.PLAYER), limit=0)

    assert hidden.status_code == 422
    assert hidden.json() == unknown.json()
