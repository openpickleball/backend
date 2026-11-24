"""Batch and submitter checks read the stored row, not the instance (#205).

``record_match`` compares a Submission Batch's stored submitter, Event,
and Club, so an instance changed in memory, or one naming a row that does
not exist, cannot link a match into another submitter's batch. The bulk
void services refuse a batch or submitter with no row, and
``submitter_matches`` refuses anything but an Account or a Partner
Application. Made-up data only.
"""

import re
from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, SubmissionBatch
from apps.matches.services import BATCH_REFUSED, record_match
from apps.matches.workflow import (
    submitter_matches,
    void_submission_batch,
    void_submitter_matches,
)
from apps.partners.tests.factories import PartnerApplicationFactory
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."
MISSING_PK = 987654321


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(
    players: list[Player], event: Event, user: User, batch: SubmissionBatch
) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 6, 1),
        event=event,
        submitted_by_user=user,
        submission_batch=batch,
    ).match


def _missing_batch(user: User) -> SubmissionBatch:
    batch = SubmissionBatch(pk=MISSING_PK, submitted_by_user=user)
    batch._state.adding = False
    return batch


def _assert_refused(
    players: list[Player], event: Event, user: User, batch: SubmissionBatch
) -> None:
    matches = Match.objects.count()
    events = AuditEvent.objects.count()

    with pytest.raises(ValueError, match=re.escape(BATCH_REFUSED)):
        _record(players, event, user, batch)

    assert Match.objects.count() == matches
    assert AuditEvent.objects.count() == events


def test_a_batch_changed_in_memory_is_refused(event: Event) -> None:
    owner, other = _member(event), _member(event)
    stored = SubmissionBatch.objects.create(submitted_by_user=owner, event=event)
    batch = SubmissionBatch.objects.get(pk=stored.pk)
    batch.submitted_by_user = other
    players = [PlayerFactory.create() for _ in range(2)]

    _assert_refused(players, event, other, batch)


def test_a_batch_whose_event_changed_in_memory_is_refused(event: Event) -> None:
    owner = _member(event)
    elsewhere = EventFactory.create()
    stored = SubmissionBatch.objects.create(submitted_by_user=owner, event=elsewhere)
    batch = SubmissionBatch.objects.get(pk=stored.pk)
    batch.event = event
    players = [PlayerFactory.create() for _ in range(2)]

    _assert_refused(players, event, owner, batch)


def test_a_batch_with_no_row_is_refused(event: Event) -> None:
    member = _member(event)
    players = [PlayerFactory.create() for _ in range(2)]

    _assert_refused(players, event, member, _missing_batch(member))


@pytest.fixture
def moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


def test_bulk_void_of_a_batch_with_no_row_is_refused(moderator: User) -> None:
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submission batch"):
        void_submission_batch(_missing_batch(moderator), actor=moderator, reason=REASON)

    assert AuditEvent.objects.count() == before


def test_bulk_void_of_a_submitter_with_no_row_is_refused(moderator: User) -> None:
    ghost: User = UserFactory.build()
    ghost.pk = MISSING_PK
    ghost._state.adding = False
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submitter"):
        void_submitter_matches(
            ghost, recorded_since=date(2026, 1, 1), actor=moderator, reason=REASON
        )

    assert AuditEvent.objects.count() == before


def test_bulk_void_of_an_application_with_no_row_is_refused(moderator: User) -> None:
    ghost = PartnerApplicationFactory.build()
    ghost.pk = MISSING_PK
    ghost._state.adding = False
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submitter"):
        void_submitter_matches(
            ghost, recorded_since=date(2026, 1, 1), actor=moderator, reason=REASON
        )

    assert AuditEvent.objects.count() == before


def test_submitter_matches_refuses_anything_but_an_account_or_application() -> None:
    player = PlayerFactory.create()

    with pytest.raises(ValueError, match="submitter"):
        submitter_matches(player, date(2026, 1, 1))  # type: ignore[arg-type]
