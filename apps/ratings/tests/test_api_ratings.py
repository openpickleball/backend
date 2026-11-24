"""The rating lookup at /api/v1/players/{code}/ratings (issue #53).

Sign-in is required. A player's own Account always sees their current
Ratings. Another signed-in Account sees them only while the player's
``rating_visibility`` is ``signed_in`` and the player's Account is active
(``PlayerPrivacy.visible_to``). An adult's is always ``signed_in``; a
player created at 13 to 17 starts private and stays private at 18 until
they widen it, so the answer never changes on a birthday. A hidden player
gets the same 404 as an unknown or malformed code. Every rating comes
with its reliability and is labeled a demonstration rating. The lookup
shares the per-Account throttle of every signed-in operation. Made-up
data only.
"""

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from django.core.cache import cache
from django.core.serializers.json import DjangoJSONEncoder
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.matches.models import RatingFormat
from apps.players.age import age_limit_date
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
    # The throttles' counters live in the cache.
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


@pytest.fixture
def player() -> Player:
    return PlayerFactory.create()


@pytest.fixture
def viewer() -> Client:
    """Another Account, signed in."""
    return signed_in(UserFactory.create())


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


# Sign-in


def test_a_signed_out_request_gets_401(player: Player, doubles: RatingState) -> None:
    response = lookup(Client(), player.public_code)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_an_unknown_code_signed_out_also_gets_401() -> None:
    response = lookup(Client(), generate(CodeType.PLAYER))

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


# The answer


@override_settings(OPEN_PUBLIC_PROFILE_URL=PROFILE_BASE)
def test_another_account_sees_every_rating_with_reliability(
    player: Player, viewer: Client, doubles: RatingState, singles: RatingState
) -> None:
    response = lookup(viewer, player.public_code)

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


def test_the_player_sees_their_own_ratings(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    own = lookup(signed_in(player.account), player.public_code)

    assert own.status_code == 200
    assert own.json() == lookup(viewer, player.public_code).json()


def test_values_are_returned_as_stored(
    player: Player, viewer: Client, singles: RatingState
) -> None:
    body = lookup(viewer, player.public_code).json()

    entry = body["ratings"]["singles"]
    assert entry["rating"] == 4.123456789
    assert entry["reliability"] == 72.25
    assert entry["imported_reliability"] == 55.5
    assert entry["provisional"] is False
    assert entry["established"] is True
    assert entry["spec_version"] == MADE_UP_SPEC_VERSION


def test_a_rating_with_live_updates_since_the_replay_is_provisional(
    player: Player, viewer: Client
) -> None:
    make_state(
        player,
        calculated_as_of=date(2026, 3, 31),
        live_updates_since_recalculation=2,
    )

    body = lookup(viewer, player.public_code).json()

    assert body["ratings"]["doubles"]["provisional"] is True


def test_the_profile_url_follows_the_setting(player: Player, viewer: Client) -> None:
    with override_settings(OPEN_PUBLIC_PROFILE_URL="https://other.example.org/x/"):
        body = lookup(viewer, player.public_code).json()

    assert body["profile_url"] == "https://other.example.org/x/" + player.public_code


def test_every_rating_format_is_keyed_by_its_value(
    player: Player, viewer: Client
) -> None:
    for rating_format in RatingFormat.values:
        make_state(player, rating_format=rating_format)

    body = lookup(viewer, player.public_code).json()

    assert set(body["ratings"]) == set(RatingFormat.values)
    for entry in body["ratings"].values():
        assert entry["demonstration"] is True
        assert isinstance(entry["reliability"], float)


def test_a_player_with_no_rating_gets_an_empty_object(
    player: Player, viewer: Client
) -> None:
    response = lookup(viewer, player.public_code)

    assert response.status_code == 200
    assert response.json()["ratings"] == {}
    assert response.json()["demonstration"] is True


def test_another_players_rating_is_not_shown(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    other = PlayerFactory.create()
    make_state(other, rating_format=RatingFormat.SINGLES)

    body = lookup(viewer, player.public_code).json()

    assert set(body["ratings"]) == {"doubles"}


def test_the_answer_holds_no_personal_or_internal_field(
    player: Player, viewer: Client, doubles: RatingState, singles: RatingState
) -> None:
    body = lookup(viewer, player.public_code).json()

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
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    response = lookup(viewer, player.public_code.lower())

    assert response.status_code == 200
    assert response.json()["code"] == player.public_code


# Players aged 13 to 17: hidden, and still hidden at 18 until they widen it


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    monkeypatch.setattr(timezone, "now", lambda: moment)


# Born June 2008: 18 from 2026-06-30 at UTC-12, that is 12:00 UTC.
BEFORE_18 = datetime(2026, 6, 30, 11, 59, tzinfo=UTC)
AT_18 = datetime(2026, 6, 30, 12, 0, tzinfo=UTC)


def test_a_minor_is_hidden_from_another_account_like_an_unknown_code(
    viewer: Client,
) -> None:
    minor = PlayerFactory.create(minor=True)
    make_state(minor)

    hidden = lookup(viewer, minor.public_code)
    unknown = lookup(viewer, generate(CodeType.PLAYER))
    malformed = lookup(viewer, "not-a-code")

    assert hidden.status_code == 404
    assert hidden.json() == NOT_FOUND
    for response in (unknown, malformed):
        assert response.status_code == hidden.status_code
        assert response.json() == hidden.json()


def test_a_minor_sees_their_own_ratings() -> None:
    minor = PlayerFactory.create(minor=True)
    state = make_state(minor)

    response = lookup(signed_in(minor.account), minor.public_code)

    assert response.status_code == 200
    assert response.json()["ratings"] == {"doubles": rating_json(state)}


def test_the_answer_does_not_change_when_a_player_turns_18(
    monkeypatch: pytest.MonkeyPatch, viewer: Client
) -> None:
    _at(monkeypatch, BEFORE_18)
    player = PlayerFactory.create(birth_year=2008, birth_month=6)
    make_state(player)

    assert age_limit_date() == date(2026, 6, 29)
    before = lookup(viewer, player.public_code)
    _at(monkeypatch, AT_18)
    assert age_limit_date() == date(2026, 6, 30)
    after = lookup(viewer, player.public_code)

    assert before.status_code == after.status_code == 404
    assert before.json() == after.json() == NOT_FOUND


def test_a_player_who_widens_at_18_is_shown(
    monkeypatch: pytest.MonkeyPatch, viewer: Client
) -> None:
    _at(monkeypatch, BEFORE_18)
    player = PlayerFactory.create(birth_year=2008, birth_month=6)
    state = make_state(player)
    _at(monkeypatch, AT_18)

    update_privacy(
        player=player,
        actor=player.account,
        changes={"rating_visibility": Visibility.SIGNED_IN},
    )
    response = lookup(viewer, player.public_code)

    assert response.status_code == 200
    assert response.json()["ratings"] == {"doubles": rating_json(state)}


def test_a_private_setting_hides_ratings_from_others_only(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    # Only a row written past the service can hold this for an adult.
    PlayerPrivacy.objects.filter(player=player).update(
        rating_visibility=Visibility.PRIVATE
    )

    hidden = lookup(viewer, player.public_code)
    own = lookup(signed_in(player.account), player.public_code)

    assert hidden.status_code == 404
    assert hidden.json() == NOT_FOUND
    assert own.status_code == 200


# Who gets the 404


def test_inactive_unknown_and_malformed_codes_get_the_same_404(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    account = player.account
    account.is_active = False
    account.save(update_fields=["is_active"])

    unknown = lookup(viewer, generate(CodeType.PLAYER))
    answers = [
        lookup(viewer, player.public_code),
        lookup(viewer, "not-a-code"),
        lookup(viewer, generate(CodeType.MATCH)),
    ]

    assert unknown.status_code == 404
    assert unknown.json() == NOT_FOUND
    for response in answers:
        assert response.status_code == 404
        assert response.json() == unknown.json()


# Other operations under /players/ still answer


def test_search_profile_and_match_history_still_route(player: Player) -> None:
    client = signed_in(player.account)

    assert client.get("/api/v1/players/search", {"q": "zzz"}).status_code == 200
    assert client.get(f"/api/v1/players/{player.public_code}").status_code == 200
    matches = client.get(f"/api/v1/players/{player.public_code}/matches")
    assert matches.status_code == 200


# The per-Account throttle


@pytest.fixture
def _pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: PINNED_NOW))


@pytest.mark.usefixtures("_pinned_clock")
@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_the_lookup_shares_the_account_budget(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    assert lookup(viewer, player.public_code).status_code == 200
    assert viewer.get("/api/v1/accounts/me").status_code == 200

    refused = lookup(viewer, player.public_code)

    assert refused.status_code == 429
    assert refused.json() == THROTTLED
    assert refused["Retry-After"] == "30"
    assert (
        signed_in(UserFactory.create())
        .get(ratings_path(player.public_code))
        .status_code
        == 200
    )


@pytest.mark.usefixtures("_pinned_clock")
@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=1,
    API_CLIENT_IP_THROTTLE_PER_DAY=1,
    API_ACCOUNT_THROTTLE_PER_MINUTE=100,
    API_ACCOUNT_THROTTLE_PER_DAY=1_000,
)
def test_the_client_ip_throttle_does_not_count_the_lookup(
    player: Player, viewer: Client, doubles: RatingState
) -> None:
    ip = {"HTTP_DO_CONNECTING_IP": "203.0.113.7"}

    statuses = [lookup(viewer, player.public_code, **ip).status_code for _ in range(3)]

    assert statuses == [200, 200, 200]
