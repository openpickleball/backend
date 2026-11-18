"""Player search by display name at /api/v1/players/search (issue #191).

A signed-in Account finds players to pick as opponents or partners by the
start of a word in their display name. The minimum query length, the small
limit, the inactive Account filter, and the per-Account throttle keep it
from listing every player. Made-up data only.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from ninja.throttling import SimpleRateThrottle
from pydantic import ValidationError

from apps.accounts.tests.factories import UserFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/players/search"
MINUTE_LIMIT = 30
DAY_LIMIT = 300


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    # Throttle counters live in the cache.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """A throttle clock the test moves by hand: ``clock[0]`` is now."""
    now = [1_800_000_000.0]
    monkeypatch.setattr(SimpleRateThrottle, "timer", staticmethod(lambda: now[0]))
    return now


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


@pytest.fixture
def client() -> Client:
    return signed_in(UserFactory.create())


def search(client: Client, **params: Any) -> Any:
    return client.get(SEARCH, params)


def names(response: Any) -> list[str]:
    return [item["display_name"] for item in response.json()["items"]]


def player(name: str, **kwargs: Any) -> Player:
    return PlayerFactory.create(display_name=name, **kwargs)


def deactivate(target: Player) -> None:
    account = target.account
    account.is_active = False
    account.save(update_fields=["is_active"])


# Criterion 1: the operation and its auth.


def test_signed_in_account_finds_a_player(client: Client) -> None:
    found = player("Sam Rivera")

    response = search(client, q="sam")

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"code": found.public_code, "display_name": "Sam Rivera"}]
    }


def test_no_signed_in_account_gets_401() -> None:
    player("Sam Rivera")

    response = Client().get(SEARCH, {"q": "sam"})

    assert response.status_code == 401
    assert response.json() == {
        "code": "unauthorized",
        "message": "Authentication required.",
        "errors": [],
    }


# Criterion 2: q.


def _error_fields(response: Any) -> list[str]:
    return [error["field"] for error in response.json()["errors"]]


def test_missing_q_gets_422(client: Client) -> None:
    response = client.get(SEARCH)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert _error_fields(response) == ["query.q"]


@pytest.mark.parametrize("q", ["ab", "  ab  ", "   ", ""])
def test_q_under_three_characters_after_stripping_gets_422(
    client: Client, q: str
) -> None:
    response = search(client, q=q)

    assert response.status_code == 422
    assert _error_fields(response) == ["query.q"]


def test_q_is_stripped_before_matching(client: Client) -> None:
    player("Sam Rivera")

    response = search(client, q="   riv   ")

    assert response.status_code == 200
    assert names(response) == ["Sam Rivera"]


def test_q_of_64_characters_is_accepted(client: Client) -> None:
    response = search(client, q="a" * 64)

    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_q_over_64_characters_gets_422(client: Client) -> None:
    response = search(client, q="a" * 65)

    assert response.status_code == 422
    assert _error_fields(response) == ["query.q"]


@pytest.mark.parametrize("q", ["abc\x00", "ab\ncd", "ab\x7fcd", "\x1babc"])
def test_q_with_a_control_character_gets_422_not_500(client: Client, q: str) -> None:
    client.raise_request_exception = False

    response = search(client, q=q)

    assert response.status_code == 422
    assert _error_fields(response) == ["query.q"]


def test_q_with_a_lone_surrogate_is_refused() -> None:
    # A lone surrogate cannot arrive over HTTP (Django decodes the query
    # string with replacement), so the schema is checked directly.
    from apps.players.api import PlayerSearchQuery

    with pytest.raises(ValidationError):
        PlayerSearchQuery(q="ab\ud800cd")


@pytest.mark.parametrize("q", ["ZQ", "ZQMARK" + "x" * 64, "ZQMARK\x00"])
def test_refused_q_never_appears_in_the_response(client: Client, q: str) -> None:
    response = search(client, q=q)

    assert response.status_code == 422
    assert "ZQ" not in response.content.decode()


# Criterion 3: limit.


def test_limit_defaults_to_ten(client: Client) -> None:
    for number in range(12):
        player(f"Sam Number{number:02d}")

    response = search(client, q="sam")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 10


def test_limit_of_twenty_is_accepted(client: Client) -> None:
    for number in range(22):
        player(f"Sam Number{number:02d}")

    response = search(client, q="sam", limit=20)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 20


@pytest.mark.parametrize("limit", [0, -1, 21, "many"])
def test_limit_outside_one_to_twenty_gets_422(client: Client, limit: Any) -> None:
    response = search(client, q="sam", limit=limit)

    assert response.status_code == 422
    assert _error_fields(response) == ["query.limit"]


def test_limit_of_one_returns_one(client: Client) -> None:
    player("Sam Rivera")
    player("Sam Rivas")

    assert len(search(client, q="sam", limit=1).json()["items"]) == 1


# Criterion 4: matching.


def test_a_later_word_matches(client: Client) -> None:
    player("Sam Rivera")

    assert names(search(client, q="riv")) == ["Sam Rivera"]


def test_a_word_after_a_hyphen_matches(client: Client) -> None:
    player("Jo Lee-Rivas")

    assert names(search(client, q="riv")) == ["Jo Lee-Rivas"]


def test_match_is_case_insensitive(client: Client) -> None:
    player("Sam Rivera")

    assert names(search(client, q="RIV")) == ["Sam Rivera"]
    assert names(search(client, q="sAm")) == ["Sam Rivera"]


def test_middle_of_a_word_does_not_match(client: Client) -> None:
    player("Sam Rivera")

    assert names(search(client, q="ive")) == []
    assert names(search(client, q="era")) == []


@pytest.mark.parametrize(
    ("q", "matching", "other"),
    [
        ("ab_", "Ab_cd Lee", "Abxcd Lee"),
        ("100%", "100% Kim", "1000 Kim"),
        ("x\\y", "X\\yz Moe", "Xqyz Moe"),
    ],
)
def test_like_wildcards_match_literally(
    client: Client, q: str, matching: str, other: str
) -> None:
    player(matching)
    player(other)

    assert names(search(client, q=q)) == [matching]


# Criterion 5: who appears.


def test_player_with_inactive_account_never_appears(client: Client) -> None:
    player("Sam Rivera")
    hidden = player("Sam Rivas")
    deactivate(hidden)

    assert names(search(client, q="sam")) == ["Sam Rivera"]


def test_player_aged_13_to_17_appears(client: Client) -> None:
    player("Sam Rivera", minor=True)

    assert names(search(client, q="sam")) == ["Sam Rivera"]


def test_own_player_may_appear() -> None:
    own = player("Sam Rivera")

    response = search(signed_in(own.account), q="sam")

    assert names(response) == ["Sam Rivera"]


# Criterion 6: the response shape.


def test_items_carry_only_code_and_display_name(client: Client) -> None:
    player("Sam Rivera")
    player("Sam Rivas")

    content = search(client, q="sam").json()

    assert set(content) == {"items"}
    assert all(set(item) == {"code", "display_name"} for item in content["items"])


# Criterion 7: order.


def test_whole_name_prefix_first_then_name_then_code(client: Client) -> None:
    later_b = player("Sam Rivera")
    later_a = player("ann rivers")
    first_b = player("rivet Al")
    first_a = player("Rivera Sam")
    twins = sorted(
        (player("Rivas Twin"), player("Rivas Twin")), key=lambda p: p.public_code
    )

    response = search(client, q="riv")

    assert [item["code"] for item in response.json()["items"]] == [
        twins[0].public_code,
        twins[1].public_code,
        first_a.public_code,
        first_b.public_code,
        later_a.public_code,
        later_b.public_code,
    ]


# Criterion 8: a fixed number of queries.


def _query_count(client: Client) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = search(client, q="sam", limit=20)
    assert response.status_code == 200
    return len(queries)


def test_query_count_does_not_grow_with_matches(client: Client) -> None:
    player("Sam One")
    one = _query_count(client)
    for number in range(10):
        player(f"Sam Number{number}")

    assert _query_count(client) == one


# Criterion 9: throttling.


def _throttled(response: Any) -> None:
    assert response.status_code == 429
    assert response.json() == {
        "code": "throttled",
        "message": "Too many requests.",
        "errors": [],
    }
    assert int(response["Retry-After"]) > 0


def test_31st_request_in_a_minute_gets_429(client: Client, clock: list[float]) -> None:
    for _ in range(MINUTE_LIMIT):
        assert search(client, q="sam").status_code == 200

    _throttled(search(client, q="sam"))


def test_minute_limit_resets_after_a_minute(client: Client, clock: list[float]) -> None:
    for _ in range(MINUTE_LIMIT):
        search(client, q="sam")
    clock[0] += 61

    assert search(client, q="sam").status_code == 200


def test_301st_request_in_a_day_gets_429(client: Client, clock: list[float]) -> None:
    for _ in range(DAY_LIMIT // MINUTE_LIMIT):
        for _ in range(MINUTE_LIMIT):
            assert search(client, q="sam").status_code == 200
        clock[0] += 61

    _throttled(search(client, q="sam"))


def test_refused_422_requests_count(client: Client, clock: list[float]) -> None:
    for _ in range(MINUTE_LIMIT):
        assert search(client, q="ab").status_code == 422

    _throttled(search(client, q="sam"))


def test_accounts_have_separate_counters(client: Client, clock: list[float]) -> None:
    for _ in range(MINUTE_LIMIT):
        search(client, q="sam")
    _throttled(search(client, q="sam"))

    other = signed_in(UserFactory.create())

    assert search(other, q="sam").status_code == 200


def test_401_requests_do_not_count(clock: list[float]) -> None:
    anonymous = Client()
    for _ in range(MINUTE_LIMIT + 1):
        assert anonymous.get(SEARCH, {"q": "sam"}).status_code == 401

    client = signed_in(UserFactory.create())
    for _ in range(MINUTE_LIMIT):
        assert search(client, q="sam").status_code == 200


def test_throttle_is_keyed_by_account_pk_not_email(
    client: Client, clock: list[float]
) -> None:
    account = UserFactory.create()
    search(signed_in(account), q="sam")

    assert len(cache.get(f"throttle_player_search_minute_{account.pk}")) == 1
    assert len(cache.get(f"throttle_player_search_day_{account.pk}")) == 1
    assert cache.get(f"throttle_player_search_minute_{account.email}") is None


# Criterion 10: the OpenAPI document.


def test_operation_is_documented() -> None:
    document = Client().get("/api/v1/openapi.json").json()
    operation = document["paths"][SEARCH]["get"]

    assert operation["operationId"] == "search_players"
    assert {"200", "401", "422", "429"} <= set(operation["responses"])
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert set(parameters) == {"q", "limit"}
    assert parameters["q"]["required"] is True
    assert parameters["q"]["schema"]["minLength"] == 3
    assert parameters["q"]["schema"]["maxLength"] == 64
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 20


# Criterion 11: the match history route still works.


def test_match_history_route_is_unaffected() -> None:
    own = player("Sam Rivera")

    response = signed_in(own.account).get(f"/api/v1/players/{own.public_code}/matches")

    assert response.status_code == 200
    assert response.json() == {"items": [], "count": 0}


def test_search_is_not_read_as_a_player_code(client: Client) -> None:
    response = client.get("/api/v1/players/search/matches")

    assert response.status_code == 404
