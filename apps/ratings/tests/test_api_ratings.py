"""The public rating lookup at /api/v1/players/{code}/ratings (issue #53).

No sign-in: anyone may read the current Ratings of a player whose Account
is active and who is 18 or older on the age limit date (``is_adult``,
ADR 0017). Anyone else, and an unknown or malformed code, gets the same
404. The view ignores any cookie or token, so a signed-in request gets
the same answer, and it never sets a cookie. Every rating comes with its
reliability and is labeled a demonstration rating. Its only throttle is
per client IP. Made-up data only.
"""

import json
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.test import Client, override_settings

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import RatingFormat
from apps.players.age import age_limit_date, is_minor
from apps.players.models import Player, PlayerPrivacy, Visibility
from apps.players.services import update_privacy
from apps.players.tests.factories import PlayerFactory
from apps.public_ids.codes import CodeType, generate
from apps.ratings.models import RatingState
from apps.ratings.tests.factories import MADE_UP_SPEC_VERSION, make_state
from config.throttling import FixedWindowThrottle

pytestmark = pytest.mark.django_db

PROFILE_BASE = "https://players.example.org/p/"
NOT_FOUND = {"code": "not_found", "message": "Not found.", "errors": []}
THROTTLED = {"code": "throttled", "message": "Too many requests.", "errors": []}
# Eight hours and thirty seconds into a UTC day: no window edge nearby.
PINNED_NOW = float(20_833 * 86_400 + 8 * 3_600 + 30)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    # The client IP throttle's counters live in the cache.
    cache.clear()
    yield
    cache.clear()


def ratings_path(code: str) -> str:
    return f"/api/v1/players/{code}/ratings"


def lookup(client: Client, code: str, **headers: Any) -> Any:
    return client.get(ratings_path(code), **headers)


def signed_in(account: Any) -> Client:
    client = Client()
    client.force_login(account)
    return client


def encoded(value: object) -> object:
    """``value`` as the API's JSON encoder writes it."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def rating_json(state: RatingState) -> dict[str, Any]:
    state.refresh_from_db()
    return {
        "rating": state.rating,
        "reliability": state.reliability,
        "imported_reliability": state.imported_reliability,
        "provisional": state.provisional,
        "established": state.established,
        "spec_version": state.spec_version,
        "demonstration": True,
        "updated_at": encoded(state.updated_at),
    }


def assert_no_cookie(response: Any) -> None:
    assert "Set-Cookie" not in response.headers
    assert not response.cookies


@pytest.fixture
def player() -> Player:
    return PlayerFactory.create()


@pytest.fixture
def doubles(player: Player) -> RatingState:
    # Never replayed, so provisional.
    return make_state(player)


@pytest.fixture
def singles(player: Player) -> RatingState:
    # Replayed, established, with an imported reliability.
    return make_state(
        player,
        rating_format=RatingFormat.SINGLES,
        rating=4.123456789,
        reliability=72.25,
        imported_reliability=55.5,
        established=True,
        calculated_as_of=date(2026, 3, 31),
        live_updates_since_recalculation=0,
    )


# The answer


@override_settings(OPEN_PUBLIC_PROFILE_URL=PROFILE_BASE)
def test_anonymous_lookup_returns_every_rating_with_reliability(
    player: Player, doubles: RatingState, singles: RatingState
) -> None:
    response = lookup(Client(), player.public_code)

    assert response.status_code == 200
    assert response.json() == {
        "code": player.public_code,
        "profile_url": PROFILE_BASE + player.public_code,
        "demonstration": True,
        "ratings": {
            "doubles": rating_json(doubles),
            "singles": rating_json(singles),
        },
    }


def test_values_are_returned_as_stored(player: Player, singles: RatingState) -> None:
    body = lookup(Client(), player.public_code).json()

    entry = body["ratings"]["singles"]
    assert entry["rating"] == 4.123456789
    assert entry["reliability"] == 72.25
    assert entry["imported_reliability"] == 55.5
    assert entry["provisional"] is False
    assert entry["established"] is True
    assert entry["spec_version"] == MADE_UP_SPEC_VERSION


def test_a_rating_with_live_updates_since_the_replay_is_provisional(
    player: Player,
) -> None:
    make_state(
        player,
        calculated_as_of=date(2026, 3, 31),
        live_updates_since_recalculation=2,
    )

    body = lookup(Client(), player.public_code).json()

    assert body["ratings"]["doubles"]["provisional"] is True


def test_the_profile_url_follows_the_setting(player: Player) -> None:
    with override_settings(OPEN_PUBLIC_PROFILE_URL="https://other.example.org/x/"):
        body = lookup(Client(), player.public_code).json()

    assert body["profile_url"] == "https://other.example.org/x/" + player.public_code


def test_every_rating_format_is_keyed_by_its_value(player: Player) -> None:
    for rating_format in RatingFormat.values:
        make_state(player, rating_format=rating_format)

    body = lookup(Client(), player.public_code).json()

    assert set(body["ratings"]) == set(RatingFormat.values)
    for entry in body["ratings"].values():
        assert entry["demonstration"] is True
        assert isinstance(entry["reliability"], float)


def test_a_player_with_no_rating_gets_an_empty_object(player: Player) -> None:
    response = lookup(Client(), player.public_code)

    assert response.status_code == 200
    assert response.json()["ratings"] == {}
    assert response.json()["demonstration"] is True


def test_another_players_rating_is_not_shown(
    player: Player, doubles: RatingState
) -> None:
    other = PlayerFactory.create()
    make_state(other, rating_format=RatingFormat.SINGLES)

    body = lookup(Client(), player.public_code).json()

    assert set(body["ratings"]) == {"doubles"}


def test_the_answer_holds_no_personal_or_internal_field(
    player: Player, doubles: RatingState, singles: RatingState
) -> None:
    body = lookup(Client(), player.public_code).json()

    assert set(body) == {"code", "profile_url", "demonstration", "ratings"}
    for entry in body["ratings"].values():
        assert set(entry) == {
            "rating",
            "reliability",
            "imported_reliability",
            "provisional",
            "established",
            "spec_version",
            "demonstration",
            "updated_at",
        }
    assert player.display_name not in json.dumps(body)


def test_a_lowercase_code_finds_the_player(
    player: Player, doubles: RatingState
) -> None:
    response = lookup(Client(), player.public_code.lower())

    assert response.status_code == 200
    assert response.json()["code"] == player.public_code


# Cookies and sign-in


def test_an_anonymous_lookup_sets_no_cookie(
    player: Player, doubles: RatingState
) -> None:
    response = lookup(Client(), player.public_code)

    assert response.status_code == 200
    assert_no_cookie(response)


def test_a_signed_in_lookup_gets_the_same_answer_and_no_cookie(
    player: Player, doubles: RatingState
) -> None:
    anonymous = lookup(Client(), player.public_code)
    other = lookup(signed_in(UserFactory.create()), player.public_code)
    own = lookup(signed_in(player.account), player.public_code)

    for response in (other, own):
        assert response.status_code == 200
        assert response.json() == anonymous.json()
        assert_no_cookie(response)


def test_an_app_session_token_is_ignored(player: Player, doubles: RatingState) -> None:
    anonymous = lookup(Client(), player.public_code)

    response = lookup(
        Client(), player.public_code, HTTP_X_SESSION_TOKEN="made-up-not-a-token"
    )

    assert response.status_code == 200
    assert response.json() == anonymous.json()
    assert_no_cookie(response)


# Who is hidden: always the same 404


def _deactivate(player: Player) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])


def _open_every_setting(player: Player) -> None:
    update_privacy(
        player=player,
        actor=player.account,
        changes=dict.fromkeys(PlayerPrivacy.SETTINGS, Visibility.SIGNED_IN),
    )


def _birth_turning_18(months_from_now: int) -> tuple[int, int]:
    """The birth year and month of a player whose 18th birthday month is
    ``months_from_now`` months from this month (negative: in the past)."""
    today = age_limit_date()
    index = today.year * 12 + today.month - 1 + months_from_now
    return index // 12 - 18, index % 12 + 1


def test_unknown_malformed_inactive_and_minor_players_get_the_same_404(
    player: Player, doubles: RatingState
) -> None:
    _deactivate(player)
    minor = PlayerFactory.create(minor=True)
    make_state(minor)
    opened_minor = PlayerFactory.create(minor=True)
    make_state(opened_minor)
    _open_every_setting(opened_minor)

    unknown = lookup(Client(), generate(CodeType.PLAYER))
    answers = [
        lookup(Client(), player.public_code),
        lookup(Client(), minor.public_code),
        lookup(Client(), opened_minor.public_code),
        lookup(Client(), "not-a-code"),
        lookup(Client(), generate(CodeType.MATCH)),
    ]

    assert unknown.status_code == 404
    assert unknown.json() == NOT_FOUND
    for response in answers:
        assert response.status_code == unknown.status_code
        assert response.json() == unknown.json()
        assert_no_cookie(response)


def test_a_minor_is_hidden_even_from_their_own_account() -> None:
    minor = PlayerFactory.create(minor=True)
    make_state(minor)
    _open_every_setting(minor)

    response = lookup(signed_in(minor.account), minor.public_code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_a_player_who_turned_18_is_shown() -> None:
    year, month = _birth_turning_18(-1)
    assert not is_minor(year, month)
    player = PlayerFactory.create(birth_year=year, birth_month=month)
    make_state(player)

    response = lookup(Client(), player.public_code)

    assert response.status_code == 200
    assert set(response.json()["ratings"]) == {"doubles"}


def test_a_player_turning_18_next_month_is_hidden() -> None:
    year, month = _birth_turning_18(1)
    assert is_minor(year, month)
    player = PlayerFactory.create(birth_year=year, birth_month=month)
    make_state(player)
    _open_every_setting(player)

    response = lookup(Client(), player.public_code)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


# Other operations under /players/ still answer


def test_search_profile_and_match_history_still_route(player: Player) -> None:
    client = signed_in(player.account)

    assert client.get("/api/v1/players/search", {"q": "zzz"}).status_code == 200
    assert client.get(f"/api/v1/players/{player.public_code}").status_code == 200
    matches = client.get(f"/api/v1/players/{player.public_code}/matches")
    assert matches.status_code == 200


# The client IP throttle


@pytest.fixture
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: PINNED_NOW))


@pytest.mark.usefixtures("_pinned_clock")
@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_past_the_client_ip_limit_is_429_with_retry_after(
    player: Player, doubles: RatingState
) -> None:
    client = Client()
    first_ip = {"HTTP_DO_CONNECTING_IP": "203.0.113.7"}
    statuses = [
        lookup(client, player.public_code, **first_ip).status_code for _ in range(2)
    ]
    assert statuses == [200, 200]

    refused = lookup(client, player.public_code, **first_ip)

    assert refused.status_code == 429
    assert refused.json() == THROTTLED
    assert refused["Retry-After"] == "30"
    assert_no_cookie(refused)
    other_ip = {"HTTP_DO_CONNECTING_IP": "203.0.113.8"}
    assert lookup(client, player.public_code, **other_ip).status_code == 200


@pytest.mark.usefixtures("_pinned_clock")
@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=1,
    API_CLIENT_IP_THROTTLE_PER_DAY=100,
    API_ACCOUNT_THROTTLE_PER_MINUTE=1,
    API_ACCOUNT_THROTTLE_PER_DAY=1,
)
def test_a_signed_in_lookup_does_not_use_the_account_budget(
    player: Player, doubles: RatingState
) -> None:
    client = signed_in(player.account)
    ip = {"HTTP_DO_CONNECTING_IP": "198.51.100.4"}
    assert lookup(client, player.public_code, **ip).status_code == 200

    assert client.get("/api/v1/accounts/me").status_code == 200


@pytest.mark.usefixtures("_pinned_clock")
@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=1, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_refused_codes_count_against_the_client_ip_limit() -> None:
    ip = {"HTTP_DO_CONNECTING_IP": "198.51.100.9"}
    assert lookup(Client(), generate(CodeType.PLAYER), **ip).status_code == 404

    response = lookup(Client(), "not-a-code", **ip)

    assert response.status_code == 429
    assert response.json() == THROTTLED
