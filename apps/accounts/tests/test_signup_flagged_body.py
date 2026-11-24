"""A flagged session is refused before its body is read (#114, #223).

Whatever a flagged session sends to signup or to the age check, even a
body that is not a JSON object, it gets the same fixed refusal. Made-up
data only.
"""

from typing import Any

import pytest

from apps.accounts.tests.headless import (
    APP,
    BROWSER,
    HeadlessSession,
    app_headers,
    csrf_header,
)
from apps.players.age import age_limit_date
from apps.players.signup import SIGNUP_REFUSED_SESSION_KEY

pytestmark = pytest.mark.django_db

REFUSAL = [
    {
        "message": "We can't create an account with these details.",
        "code": "signup_refused",
    }
]


def _young() -> dict[str, int]:
    return {"birth_year": age_limit_date().year - 5, "birth_month": 6}


def _post_raw(session: HeadlessSession, endpoint: str, body: bytes) -> Any:
    if session.base == BROWSER:
        headers = csrf_header(session.client)
    else:
        headers = app_headers(session.token)
    return session.client.post(
        f"{session.base}/{endpoint}",
        body,
        content_type="application/json",
        headers=headers,
    )


@pytest.mark.parametrize("base", [APP, BROWSER])
@pytest.mark.parametrize("endpoint", ["auth/signup", "auth/signup/age-check"])
@pytest.mark.parametrize("flagged_by", ["auth/signup", "auth/signup/age-check"])
@pytest.mark.parametrize(
    "body", [b"not json", b"[]", b'"x"', b"null", b"{", b"\xff\xfe"]
)
def test_a_flagged_session_is_refused_whatever_body_it_sends(
    base: str, endpoint: str, flagged_by: str, body: bytes
) -> None:
    session = HeadlessSession(base)
    if flagged_by == "auth/signup":
        first = session.signup(**_young())
    else:
        first = session.age_check(**_young())
    assert first.status_code == 400
    assert session.store()[SIGNUP_REFUSED_SESSION_KEY] is True

    response = _post_raw(session, endpoint, body)

    assert response.status_code == 400
    assert response.json()["errors"] == REFUSAL
