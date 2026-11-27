"""A data export request refused for reauthentication does not spend the
day budget (#39).

``DataExportThrottle`` ran before the reauthentication check, so the
plain client flow (request, 403, reauthenticate, request) spent two of
the three daily requests, and three stray requests locked a player out
for a day. Now only a request with a recent reauthentication counts
against ``API_DATA_EXPORT_THROTTLE_PER_DAY``; one without counts against
the per-Account budget, so refusals are still limited. Made-up data only.
"""

from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.test import override_settings

from apps.accounts.tests.factories import DEFAULT_PASSWORD
from apps.accounts.tests.headless import APP
from apps.exports.tests.test_api import NOW, REQUEST, _post, _signed_in
from config.throttling import FixedWindowThrottle

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cache.clear()
    monkeypatch.setattr(FixedWindowThrottle, "timer", staticmethod(lambda: NOW))
    yield
    cache.clear()


def test_a_refused_request_does_not_spend_the_day_budget() -> None:
    session, _ = _signed_in(APP)
    for _ in range(3):
        assert _post(session, REQUEST).status_code == 403
    assert session.reauthenticate(DEFAULT_PASSWORD).status_code == 200

    for _ in range(3):
        assert _post(session, REQUEST).status_code == 202
    assert _post(session, REQUEST).status_code == 429


@override_settings(API_ACCOUNT_THROTTLE_PER_MINUTE=2)
def test_refused_requests_count_against_the_account_budget() -> None:
    session, _ = _signed_in(APP)

    assert _post(session, REQUEST).status_code == 403
    assert _post(session, REQUEST).status_code == 403
    refused = _post(session, REQUEST)

    assert refused.status_code == 429
    assert int(refused["Retry-After"]) >= 1
