"""Bulk voids refuse an in-memory instance that names a real row (#205).

An instance built in memory with an existing row's pk is still unsaved
(``_state.adding``). The bulk void services must refuse it before any
match is voided, since the audit log refuses an unsaved target and the
run would otherwise end with an error after its writes. Made-up data only.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory, verified_organizer
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import Match, MatchFormat, MatchStatus, SubmissionBatch
from apps.matches.services import record_match
from apps.matches.workflow import void_submission_batch, void_submitter_matches
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import approved_application
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason for a test."


@pytest.fixture
def moderator() -> User:
    account: User = UserFactory.create()
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


def _record(
    event: Event,
    *,
    user: User | None = None,
    application: PartnerApplication | None = None,
    batch: SubmissionBatch | None = None,
) -> Match:
    players = [PlayerFactory.create() for _ in range(2)]
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 7), (11, 9)),
        played_on=date(2026, 6, 1),
        event=event,
        submitted_by_user=user,
        submitted_by_application=application,
        submission_batch=batch,
    ).match


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _assert_nothing_changed(match: Match, events_before: int) -> None:
    assert Match.objects.get(pk=match.pk).status == MatchStatus.CONFIRMED
    assert AuditEvent.objects.count() == events_before


def test_an_in_memory_batch_with_a_real_pk_is_refused(
    event: Event, moderator: User
) -> None:
    member = _member(event)
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    match = _record(event, user=member, batch=batch)
    copy = SubmissionBatch(pk=batch.pk, submitted_by_user=member)
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submission batch"):
        void_submission_batch(copy, actor=moderator, reason=REASON)

    _assert_nothing_changed(match, before)


def test_an_in_memory_account_with_a_real_pk_is_refused(
    event: Event, moderator: User
) -> None:
    member = _member(event)
    match = _record(event, user=member)
    copy = User(pk=member.pk, email=member.email)
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submitter"):
        void_submitter_matches(
            copy, recorded_since=date(2026, 1, 1), actor=moderator, reason=REASON
        )

    _assert_nothing_changed(match, before)


def test_an_in_memory_application_with_a_real_pk_is_refused(
    event: Event, moderator: User
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _record(event, application=application)
    copy = PartnerApplication(pk=application.pk)
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submitter"):
        void_submitter_matches(
            copy, recorded_since=date(2026, 1, 1), actor=moderator, reason=REASON
        )

    _assert_nothing_changed(match, before)
