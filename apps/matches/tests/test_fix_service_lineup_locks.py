"""Every status change takes the match's lineup lock (review finding G, #26).

Whether flag-linked matches count is read under the lineup lock, the one
``record_match`` takes, so hold, release, and change signals are neither
lost nor doubled by concurrent changes. Expiry never waits: it skips a
match whose lineup lock another session holds. Made-up data only.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import Match, MatchStatus, Team
from apps.matches.services import _lineup_lock_key
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.tests.test_fix_duplicate_race_and_edit_locks import (
    held_advisory_keys,
)
from apps.matches.workflow import (
    confirm_match,
    dispute_match,
    expire_unconfirmed_matches,
    resolve_dispute,
    void_match,
)
from apps.players.models import Player

pytestmark = pytest.mark.django_db


def _team(match: Match, team: str) -> list[Player]:
    return [p.player for p in match.participants.filter(team=team)]


def _key(match: Match) -> int:
    return _lineup_lock_key(
        [p.pk for p in _team(match, Team.A)],
        [p.pk for p in _team(match, Team.B)],
        match.played_on,
    )


def _match(status: MatchStatus = MatchStatus.PENDING, **values: Any) -> Match:
    # Built by the factory, so no earlier call in the test took its locks.
    made: Match = MatchFactory.create(**values)
    raw_update(made, status=status)
    made.refresh_from_db()
    return made


def _participant(match: Match) -> User:
    account: User = _team(match, Team.A)[0].account
    return account


def _member(event: Event) -> User:
    member: User = OrganizerMemberFactory.create(organizer=event.organizer).account
    return member


ACTIONS: dict[str, tuple[MatchStatus, Callable[[Match, User], object]]] = {
    "confirm": (
        MatchStatus.PENDING,
        lambda match, member: confirm_match(match, actor=_participant(match)),
    ),
    "dispute": (
        MatchStatus.CONFIRMED,
        lambda match, member: dispute_match(match, actor=_participant(match)),
    ),
    "resolve": (
        MatchStatus.DISPUTED,
        lambda match, member: resolve_dispute(
            match, actor=member, outcome=MatchStatus.CONFIRMED, reason="checked"
        ),
    ),
    "void": (
        MatchStatus.CONFIRMED,
        lambda match, member: void_match(match, actor=member),
    ),
}


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_each_service_takes_the_lineup_lock(action: str, event: Event) -> None:
    status, call = ACTIONS[action]
    match = _match(status, event=event)
    member = _member(event)
    assert held_advisory_keys() == set()

    call(match, member)

    assert _key(match) in held_advisory_keys()


def test_an_organizer_confirmation_takes_the_lineup_lock(event: Event) -> None:
    match = _match(event=event)
    member = _member(event)

    confirm_match(match, actor=member)

    assert _key(match) in held_advisory_keys()


@contextmanager
def _other_session_holds(key: int) -> Iterator[None]:
    """Hold an advisory lock on ``key`` from a second database session."""
    params = connection.settings_dict
    other = psycopg.connect(
        dbname=params["NAME"],
        user=params["USER"],
        password=params["PASSWORD"],
        host=params["HOST"],
        port=params["PORT"],
    )
    try:
        other.execute("SELECT pg_advisory_xact_lock(%s)", [key])
        yield
    finally:
        other.rollback()
        other.close()


@override_settings(MATCH_CONFIRMATION_EXPIRY_DAYS=7)
def test_expiry_skips_a_match_whose_lineup_lock_is_held() -> None:
    match = _match()
    raw_update(match, created_at=timezone.now() - timedelta(days=30))

    with _other_session_holds(_key(match)):
        assert expire_unconfirmed_matches() == 0
        assert Match.objects.get(pk=match.pk).status == MatchStatus.PENDING

    assert expire_unconfirmed_matches() == 1
    assert Match.objects.get(pk=match.pk).status == MatchStatus.VOIDED
    assert _key(match) in held_advisory_keys()
