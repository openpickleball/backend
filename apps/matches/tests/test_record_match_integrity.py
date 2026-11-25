"""The database backs the exact-duplicate rule, and ``record_match`` turns a
constraint that fires into ``DuplicateMatchError``, never ``IntegrityError``.
"""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from django.db import IntegrityError, transaction

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import (
    Game,
    Match,
    MatchFlag,
    MatchFormat,
    MatchParticipant,
    MatchStatus,
)
from apps.matches.services import DuplicateKind, DuplicateMatchError, record_match
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

FINGERPRINT = "a" * 64


def _bare_match(**overrides: object) -> Match:
    made: Match = MatchFactory.create(games=False, participants=False, **overrides)
    return made


def _set_fingerprint(match: Match, fingerprint: str) -> None:
    raw_update(match, fingerprint=fingerprint)


def test_two_matches_not_voided_cannot_share_a_fingerprint() -> None:
    _set_fingerprint(_bare_match(), FINGERPRINT)
    second = _bare_match()

    with pytest.raises(IntegrityError) as error, transaction.atomic():
        _set_fingerprint(second, FINGERPRINT)

    assert "matches_match_fingerprint_unique_unless_voided" in str(error.value)


def test_a_voided_match_may_share_a_fingerprint() -> None:
    _set_fingerprint(_bare_match(status=MatchStatus.VOIDED), FINGERPRINT)
    pending = _bare_match()

    _set_fingerprint(pending, FINGERPRINT)

    assert Match.objects.filter(fingerprint=FINGERPRINT).count() == 2


def test_blank_fingerprints_never_conflict() -> None:
    _bare_match()
    _bare_match()

    assert Match.objects.filter(fingerprint="").count() == 2


# record_match: a constraint that fires after the checks missed.

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7),)


def _miss_first_lookup(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """The pre-insert lookup misses, as if a concurrent insert committed
    after it ran. Later lookups see the row."""
    real: Callable[..., Match | None] = getattr(services, name)
    calls = {"count": 0}

    def lookup(*args: Any, **kwargs: Any) -> Match | None:
        calls["count"] += 1
        return None if calls["count"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(services, name, lookup)


def _record(team_a: list[Player], team_b: list[Player], **overrides: Any) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": team_a,
        "team_b": team_b,
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values).match


def _counts() -> tuple[int, int, int, int]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
    )


@pytest.fixture
def players() -> tuple[list[Player], list[Player]]:
    return [PlayerFactory.create()], [PlayerFactory.create()]


@pytest.fixture
def event() -> Event:
    """An Event of a verified Organizer (ADR 0021)."""
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _event_account(event: Event) -> User:
    """A member of ``event``'s Organizer, so it may submit there (#79)."""
    made: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=made)
    return made


def _granted_application(event: Event) -> PartnerApplication:
    """An approved application with the grant of ``event``'s Organizer."""
    made = approved_application()
    OrganizerApplicationGrantFactory.create(organizer=event.organizer, application=made)
    return made


def test_fingerprint_constraint_becomes_duplicate_match_error(
    monkeypatch: pytest.MonkeyPatch,
    players: tuple[list[Player], list[Player]],
    event: Event,
) -> None:
    team_a, team_b = players
    user = _event_account(event)
    first = _record(team_a, team_b, submitted_by_user=user, event=event)
    _miss_first_lookup(monkeypatch, "_exact_match")
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitted_by_user=user, event=event)

    assert error.value.kind == DuplicateKind.DUPLICATE_MATCH
    assert error.value.existing_code == first.public_code
    assert _counts() == before


def test_fingerprint_constraint_hides_the_code_from_an_unrelated_account(
    monkeypatch: pytest.MonkeyPatch,
    players: tuple[list[Player], list[Player]],
    event: Event,
) -> None:
    team_a, team_b = players
    _record(team_a, team_b, submitted_by_user=_event_account(event), event=event)
    _miss_first_lookup(monkeypatch, "_exact_match")

    with pytest.raises(DuplicateMatchError) as error:
        _record(team_a, team_b, submitted_by_user=_event_account(event), event=event)

    assert error.value.kind == DuplicateKind.DUPLICATE_MATCH
    assert error.value.existing_code is None


def test_external_id_constraint_becomes_duplicate_external_id_error(
    monkeypatch: pytest.MonkeyPatch,
    players: tuple[list[Player], list[Player]],
    event: Event,
) -> None:
    team_a, team_b = players
    application = _granted_application(event)
    first = _record(
        team_a,
        team_b,
        submitted_by_application=application,
        external_id="ext-1",
        event=event,
    )
    _miss_first_lookup(monkeypatch, "_external_id_match")
    before = _counts()

    with pytest.raises(DuplicateMatchError) as error:
        _record(
            [PlayerFactory.create()],
            [PlayerFactory.create()],
            submitted_by_application=application,
            external_id="ext-1",
            event=event,
        )

    assert error.value.kind == DuplicateKind.DUPLICATE_EXTERNAL_ID
    assert error.value.existing_code == first.public_code
    assert _counts() == before


def test_a_conflict_that_cannot_be_named_stays_an_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
    players: tuple[list[Player], list[Player]],
    event: Event,
) -> None:
    team_a, team_b = players
    user = _event_account(event)
    _record(team_a, team_b, submitted_by_user=user, event=event)
    # Every lookup misses, so the conflicting Match cannot be named.
    monkeypatch.setattr(services, "_exact_match", lambda fingerprint: None)

    with pytest.raises(IntegrityError), transaction.atomic():
        _record(team_a, team_b, submitted_by_user=user, event=event)
