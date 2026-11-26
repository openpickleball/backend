"""A token whose session vanishes while it is looked up is no session
(#100 review).

When last seen moves, the token strategy saves the session. If a logout
deleted it in between, Django raises ``UpdateError``: that must read as
an unknown token, not a 500. Made-up data only.
"""

from typing import Any

import pytest
from django.contrib.sessions.backends.base import UpdateError

from apps.accounts.token_strategy import SessionTokenStrategy

pytestmark = pytest.mark.django_db


def test_a_save_race_reads_as_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class Gone:
        modified = True
        session_key = "made-up-key"

        def save(self) -> None:
            raise UpdateError

    monkeypatch.setattr(
        "allauth.headless.tokens.strategies.sessions.SessionTokenStrategy.lookup_session",
        lambda self, token: Gone(),
    )
    monkeypatch.setattr(
        "apps.accounts.token_strategy.check_lifetime", lambda session: True
    )

    result: Any = SessionTokenStrategy().lookup_session("made-up-key")

    assert result is None
