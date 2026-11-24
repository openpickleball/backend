"""The fixed-window API throttles in ``config.throttling`` (issue #34).

Counters are kept in the default cache, counted with ``cache.add`` then
``cache.incr``, per scope, bucket, window name, and window index. The
Account throttle keys by the Account's primary key, the client IP
throttle by the client IP from the configured header only. Rates are read
from settings on each request. Made-up data only.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.core.cache import cache
from django.test import override_settings
from ninja.throttling import BaseThrottle, SimpleRateThrottle

from config.throttling import (
    FALLBACK_BUCKET,
    AccountThrottle,
    ClientIPThrottle,
    FixedWindowThrottle,
    account_throttle,
    client_ip_bucket,
)

DAY = 86_400
# Eight hours into a UTC day, on a minute boundary: far from either edge.
START = 20_833 * DAY + 8 * 3_600


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


class _Clock:
    def __init__(self) -> None:
        self.now = float(START)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


def _account_request(pk: int | None, email: str = "made.up@example.org") -> Any:
    return SimpleNamespace(auth=SimpleNamespace(pk=pk, email=email), META={})


def _ip_request(**meta: str) -> Any:
    return SimpleNamespace(auth=None, META=meta)


def _account_throttle(clock: _Clock) -> AccountThrottle:
    throttle = AccountThrottle("account-test")
    throttle.timer = clock
    return throttle


def _ip_throttle(clock: _Clock) -> ClientIPThrottle:
    throttle = ClientIPThrottle("ip-test")
    throttle.timer = clock
    return throttle


# The base


def test_throttles_are_not_ninja_built_in_rate_throttles() -> None:
    for throttle_class in (FixedWindowThrottle, AccountThrottle, ClientIPThrottle):
        assert issubclass(throttle_class, BaseThrottle)
        assert not issubclass(throttle_class, SimpleRateThrottle)


def test_shared_account_throttle_is_an_account_throttle() -> None:
    assert isinstance(account_throttle, AccountThrottle)


def test_instance_holds_only_scope_and_thread_local() -> None:
    throttle = AccountThrottle("account-test")

    assert set(vars(throttle)) == {"scope", "_refused"}


def test_allow_request_keeps_no_request_state_on_the_throttle(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    before = dict(vars(throttle))

    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1):
        for pk in (5_101, 5_102, 5_101):
            throttle.allow_request(_account_request(pk))

    assert dict(vars(throttle)) == before


# The Account throttle


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=3, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_account_minute_limit(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_201)

    assert [throttle.allow_request(request) for _ in range(4)] == [
        True,
        True,
        True,
        False,
    ]


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_accounts_have_separate_limits(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    for _ in range(2):
        assert throttle.allow_request(_account_request(5_301))
    assert not throttle.allow_request(_account_request(5_301))

    assert throttle.allow_request(_account_request(5_302))


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_minute_window_ends(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_401)
    for _ in range(2):
        assert throttle.allow_request(request)
    assert not throttle.allow_request(request)

    clock.now += 60

    assert throttle.allow_request(request)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2, API_ACCOUNT_THROTTLE_PER_DAY=3)
def test_minute_refusals_do_not_use_up_the_day(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_501)
    for _ in range(2):
        assert throttle.allow_request(request)
    for _ in range(10):
        assert not throttle.allow_request(request)

    clock.now += 60

    # The day has counted only the two allowed requests.
    assert throttle.allow_request(request)
    assert not throttle.allow_request(request)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=50, API_ACCOUNT_THROTTLE_PER_DAY=2)
def test_day_limit_holds_across_minutes(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_601)
    for _ in range(2):
        assert throttle.allow_request(request)
        clock.now += 60

    assert not throttle.allow_request(request)


def test_rates_are_read_on_each_request(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_701)
    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1):
        assert throttle.allow_request(request)
        assert not throttle.allow_request(request)

    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=5):
        assert throttle.allow_request(request)


def test_request_without_an_account_is_not_counted(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1):
        for request in (_ip_request(), _account_request(None)):
            for _ in range(3):
                assert throttle.allow_request(request)
                assert throttle.wait() is None

    assert not cache._cache  # type: ignore[attr-defined]


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1)
def test_keys_hold_the_primary_key_never_the_email(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    email = "keyed.player@example.org"

    throttle.allow_request(_account_request(5_801, email))

    keys = [str(key) for key in cache._cache]  # type: ignore[attr-defined]
    assert keys
    assert all("5801" in key for key in keys)
    assert not any("keyed.player" in key for key in keys)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_wait_is_seconds_until_the_minute_window_ends(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(5_901)
    clock.now += 15.25
    assert throttle.allow_request(request)
    assert throttle.wait() is None

    assert not throttle.allow_request(request)

    assert throttle.wait() == pytest.approx(44.75)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=100, API_ACCOUNT_THROTTLE_PER_DAY=1)
def test_wait_is_seconds_until_the_day_window_ends(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(6_001)
    assert throttle.allow_request(request)

    assert not throttle.allow_request(request)

    assert throttle.wait() == pytest.approx(16 * 3_600)


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_wait_is_at_least_one_second(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    request = _account_request(6_101)
    clock.now += 59.9
    assert throttle.allow_request(request)

    assert not throttle.allow_request(request)

    assert throttle.wait() == 1.0


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1, API_ACCOUNT_THROTTLE_PER_DAY=100)
def test_allowed_request_clears_the_last_wait(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    assert throttle.allow_request(_account_request(6_201))
    assert not throttle.allow_request(_account_request(6_201))

    assert throttle.allow_request(_account_request(6_202))

    assert throttle.wait() is None


# Client IP buckets


@pytest.mark.parametrize(
    ("value", "bucket"),
    [
        ("203.0.113.7", "203.0.113.7"),
        (" 203.0.113.7 ", "203.0.113.7"),
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
        ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
        ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
    ],
)
def test_client_ip_bucket_from_the_header(value: str, bucket: str) -> None:
    assert client_ip_bucket(_ip_request(HTTP_DO_CONNECTING_IP=value)) == bucket


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-an-ip",
        "203.0.113.7, 198.51.100.4",
        "203.0.113.7,198.51.100.4",
        "203.0.113.256",
        "203.0.113.7:443",
        "[2001:db8::1]",
        "unknown",
    ],
)
def test_invalid_header_value_uses_the_fallback_bucket(value: str) -> None:
    bucket = client_ip_bucket(_ip_request(HTTP_DO_CONNECTING_IP=value))

    assert bucket == FALLBACK_BUCKET


def test_missing_header_uses_the_fallback_bucket() -> None:
    assert client_ip_bucket(_ip_request()) == FALLBACK_BUCKET


def test_other_ip_headers_are_never_read() -> None:
    request = _ip_request(
        HTTP_X_FORWARDED_FOR="198.51.100.4",
        HTTP_FORWARDED="for=198.51.100.5",
        HTTP_X_REAL_IP="198.51.100.6",
        REMOTE_ADDR="198.51.100.7",
    )

    assert client_ip_bucket(request) == FALLBACK_BUCKET


def test_header_ip_wins_over_other_headers() -> None:
    request = _ip_request(
        HTTP_DO_CONNECTING_IP="203.0.113.7",
        HTTP_X_FORWARDED_FOR="198.51.100.4",
        REMOTE_ADDR="198.51.100.7",
    )

    assert client_ip_bucket(request) == "203.0.113.7"


@override_settings(CLIENT_IP_HEADER="cf-connecting-ip")
def test_configured_header_is_read() -> None:
    request = _ip_request(
        HTTP_CF_CONNECTING_IP="203.0.113.8", HTTP_DO_CONNECTING_IP="203.0.113.7"
    )

    assert client_ip_bucket(request) == "203.0.113.8"


@override_settings(CLIENT_IP_HEADER="cf-connecting-ip")
def test_default_header_is_not_read_when_another_is_configured() -> None:
    request = _ip_request(HTTP_DO_CONNECTING_IP="203.0.113.7")

    assert client_ip_bucket(request) == FALLBACK_BUCKET


# The client IP throttle


@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_client_ips_have_separate_limits(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    first = _ip_request(HTTP_DO_CONNECTING_IP="203.0.113.7")
    for _ in range(2):
        assert throttle.allow_request(first)
    assert not throttle.allow_request(first)

    assert throttle.allow_request(_ip_request(HTTP_DO_CONNECTING_IP="203.0.113.8"))


@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_addresses_in_one_ipv6_64_share_a_limit(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    for value in ("2001:db8:1:2::1", "2001:db8:1:2::2"):
        assert throttle.allow_request(_ip_request(HTTP_DO_CONNECTING_IP=value))

    refused = _ip_request(HTTP_DO_CONNECTING_IP="2001:db8:1:2:aaaa::3")
    assert not throttle.allow_request(refused)
    other = _ip_request(HTTP_DO_CONNECTING_IP="2001:db8:1:3::1")
    assert throttle.allow_request(other)


@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_forged_forwarded_for_does_not_change_the_limit(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    for forged in ("198.51.100.1", "198.51.100.2"):
        request = _ip_request(
            HTTP_DO_CONNECTING_IP="203.0.113.7", HTTP_X_FORWARDED_FOR=forged
        )
        assert throttle.allow_request(request)

    request = _ip_request(
        HTTP_DO_CONNECTING_IP="203.0.113.7", HTTP_X_FORWARDED_FOR="198.51.100.3"
    )
    assert not throttle.allow_request(request)


@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=2, API_CLIENT_IP_THROTTLE_PER_DAY=100
)
def test_requests_without_the_header_share_the_fallback_limit(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    for forged in ("198.51.100.1", "198.51.100.2"):
        assert throttle.allow_request(_ip_request(HTTP_X_FORWARDED_FOR=forged))

    assert not throttle.allow_request(_ip_request(HTTP_X_FORWARDED_FOR="198.51.100.3"))
    assert not throttle.allow_request(_ip_request(HTTP_DO_CONNECTING_IP="garbage"))


def test_client_ip_rates_are_read_on_each_request(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    request = _ip_request(HTTP_DO_CONNECTING_IP="203.0.113.9")
    with override_settings(API_CLIENT_IP_THROTTLE_PER_MINUTE=1):
        assert throttle.allow_request(request)
        assert not throttle.allow_request(request)

    with override_settings(API_CLIENT_IP_THROTTLE_PER_MINUTE=5):
        assert throttle.allow_request(request)


@override_settings(
    API_CLIENT_IP_THROTTLE_PER_MINUTE=100, API_CLIENT_IP_THROTTLE_PER_DAY=1
)
def test_client_ip_day_limit(clock: _Clock) -> None:
    throttle = _ip_throttle(clock)
    request = _ip_request(HTTP_DO_CONNECTING_IP="203.0.113.10")
    assert throttle.allow_request(request)
    clock.now += 3_600

    assert not throttle.allow_request(request)


# Counting


class _FailingIncr:
    """A cache whose counter vanishes before every ``incr``."""

    def __init__(self) -> None:
        self.adds = 0

    def add(self, key: str, value: int, timeout: int) -> bool:
        self.adds += 1
        return True

    def incr(self, key: str) -> int:
        raise ValueError(key)


def test_counter_lost_twice_allows_the_request(clock: _Clock) -> None:
    throttle = _account_throttle(clock)
    failing = _FailingIncr()
    throttle.cache = failing  # type: ignore[assignment]

    with override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1):
        assert throttle.allow_request(_account_request(6_301))

    # Two tries per window, for the minute and the day.
    assert failing.adds == 4


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=1)
def test_counters_expire_after_their_window_and_a_margin(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[int] = []
    real_add = cache.add

    def add(key: str, value: int, timeout: int) -> bool:
        timeouts.append(timeout)
        return bool(real_add(key, value, timeout))

    monkeypatch.setattr(cache, "add", add)
    throttle = _account_throttle(clock)

    throttle.allow_request(_account_request(6_401))

    assert timeouts == [60 + 60, DAY + 60]
