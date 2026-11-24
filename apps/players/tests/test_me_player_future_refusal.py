"""A future birth year and month on Player create refuses the session (#225).

Signup refuses a future birth year and month and flags the session (ADR
0017). ``POST /api/v1/me/player`` now does the same: the same fixed 422
as an under-13 age, the signup refusal flag set, and every later create
in that session refused, whatever age it sends. On both headless clients.
Made-up data only.
"""

from collections.abc import Callable, Iterator

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.tests.headless import APP, BROWSER
from apps.players.age import age_limit_date
from apps.players.models import Player
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY
from apps.players.tests.test_me_player_api import call
from apps.players.tests.test_me_player_refusal import (
    ADULT_BODY,
    DIVISION,
    REFUSED_ERRORS,
    SIGNUP_REFUSAL,
    log_in,
    log_out,
    logged_in_session,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("commit_after_each_request"),
]

CLIENTS = [APP, BROWSER]


@pytest.fixture(autouse=True)
def _clear_rate_limits() -> Iterator[None]:
    """allauth keeps its rate limits in the cache: start each test empty."""
    cache.clear()
    yield
    cache.clear()


def next_year_body() -> dict[str, object]:
    """A birth month next year: in the future on every clock."""
    return {
        "display_name": "Robin Q.",
        "birth_year": age_limit_date().year + 1,
        "birth_month": 1,
        **DIVISION,
    }


def next_month_body() -> dict[str, object]:
    """The month after this one (UTC), the nearest future month."""
    today = timezone.localdate()
    year, month = (
        (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    )
    return {
        "display_name": "Robin Q.",
        "birth_year": year,
        "birth_month": month,
        **DIVISION,
    }


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize("body", [next_year_body, next_month_body])
def test_a_future_month_gets_the_refusal_and_flags_the_session(
    base: str, body: Callable[[], dict[str, object]]
) -> None:
    session = logged_in_session(base)

    response = call(session, "POST", body())

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS
    assert session.store().get(SIGNUP_REFUSED_SESSION_KEY) is True
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_an_adult_retry_after_a_future_month_gets_the_same_answer(base: str) -> None:
    session = logged_in_session(base)

    first = call(session, "POST", next_year_body())
    retry = call(session, "POST", ADULT_BODY)

    assert retry.status_code == first.status_code == 422
    assert retry.content == first.content
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
def test_the_future_refusal_hides_other_errors(base: str) -> None:
    session = logged_in_session(base)

    response = call(session, "POST", {**next_year_body(), "display_name": " "})

    assert response.status_code == 422
    assert response.json()["errors"] == REFUSED_ERRORS


@pytest.mark.parametrize("base", CLIENTS)
def test_a_future_month_refusal_survives_logout_and_blocks_signup(base: str) -> None:
    session = logged_in_session(base)
    call(session, "POST", next_year_body())
    log_out(session)

    signup = session.signup("new.player@example.org")
    log_in(session)
    create = call(session, "POST", ADULT_BODY)

    assert signup.status_code == 400
    assert signup.json()["errors"] == SIGNUP_REFUSAL
    assert create.status_code == 422
    assert create.json()["errors"] == REFUSED_ERRORS
    assert not Player.objects.exists()


@pytest.mark.parametrize("base", CLIENTS)
@pytest.mark.parametrize(
    "birth",
    [
        pytest.param(lambda: (timezone.localdate().year, 13), id="month-13-this-year"),
        pytest.param(lambda: (age_limit_date().year + 1, 0), id="month-0-next-year"),
    ],
)
def test_a_malformed_month_is_not_a_refusal(
    base: str, birth: Callable[[], tuple[int, int]]
) -> None:
    # A month out of range is malformed, not in the future: its own 422,
    # and no flag (ADR 0017).
    session = logged_in_session(base)
    year, month = birth()

    refused = call(
        session, "POST", {**ADULT_BODY, "birth_year": year, "birth_month": month}
    )
    created = call(session, "POST", ADULT_BODY)

    assert refused.status_code == 422
    assert refused.json()["errors"] != REFUSED_ERRORS
    assert SIGNUP_REFUSED_SESSION_KEY not in session.store()
    assert created.status_code == 201, created.json()
