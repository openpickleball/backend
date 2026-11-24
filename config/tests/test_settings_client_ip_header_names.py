"""CLIENT_IP_HEADER refuses names that can never match (issue #34 review).

WSGI puts Content-Type and Content-Length in ``CONTENT_TYPE`` and
``CONTENT_LENGTH``, without the ``HTTP_`` prefix, and no header name
starts or ends with a hyphen. Such a setting would put every request in
the fallback bucket without a word, so startup refuses it, naming the
variable and never echoing the value.
"""

import pytest

from config.tests.test_settings_throttle import (
    HEADER,
    _assert_refused,
    _throttle_settings,
)


@pytest.mark.parametrize(
    "value",
    ["content-type", "Content-Length", "-", "--", "-ip", "ip-", "-do-connecting-ip"],
)
def test_a_header_that_never_matches_stops_startup(value: str) -> None:
    _assert_refused(HEADER, value)


@pytest.mark.parametrize("value", ["do-connecting-ip", "x-real-ip", "ip", "x-a1"])
def test_a_usable_header_name_is_accepted(value: str) -> None:
    assert _throttle_settings({HEADER: value})[HEADER] == value
