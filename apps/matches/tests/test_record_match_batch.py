"""``record_match`` with a Submission Batch (#205). Made-up data only.

A batch path passes its batch to ``record_match``, which links the new
match to it. A batch that does not fit the submission (unsaved, another
submitter, another Event or Club) is refused with fixed text after the
permission checks and before validation, and nothing is written.
"""

import re
from collections.abc import Sequence
from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.clubs.models import Club
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchImmutable,
    MatchStatus,
    SubmissionBatch,
)
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import (
    BATCH_REFUSED,
    DuplicateKind,
    DuplicateMatchError,
    record_match,
)
from apps.matches.tests.conftest import SignalLog
from apps.matches.workflow import edit_match
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (9, 11), (11, 4))
INVALID_GAMES = ((11, 10),)


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(2)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


@pytest.fixture
def application(event: Event) -> PartnerApplication:
    return approved_application(organizer=event.organizer)


def _record(players: Sequence[Player], **overrides: Any) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": players[:1],
        "team_b": players[1:],
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    values.update(overrides)
    return record_match(**values).match


def _counts() -> tuple[int, int, int]:
    return (
        Match.objects.count(),
        AuditEvent.objects.count(),
        SubmissionBatch.objects.count(),
    )


# The link


def test_a_match_recorded_with_a_batch_links_to_it(
    players: list[Player], event: Event, member: User
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)

    match = _record(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )

    assert Match.objects.get(pk=match.pk).submission_batch_id == batch.pk
    assert list(batch.matches.all()) == [match]


def test_a_match_recorded_without_a_batch_has_none(
    players: list[Player], event: Event, member: User
) -> None:
    match = _record(players, event=event, submitted_by_user=member)

    assert Match.objects.get(pk=match.pk).submission_batch_id is None


def test_an_application_batch_links_its_matches(
    players: list[Player], event: Event, application: PartnerApplication
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_application=application)

    match = _record(
        players,
        event=event,
        submitted_by_application=application,
        submission_batch=batch,
    )

    assert Match.objects.get(pk=match.pk).submission_batch_id == batch.pk


def test_a_self_submission_can_be_in_a_batch(players: list[Player]) -> None:
    account = players[0].account
    batch = SubmissionBatch.objects.create(submitted_by_user=account)

    match = _record(players, submitted_by_user=account, submission_batch=batch)

    assert Match.objects.get(pk=match.pk).submission_batch_id == batch.pk


def test_a_batch_with_no_event_accepts_any_event(
    players: list[Player], event: Event, member: User
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_user=member)

    match = _record(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )

    assert match.submission_batch_id == batch.pk


def _granting_club(event: Event) -> Club:
    """A Club that granted ``event``'s Organizer (ADR 0021)."""
    club: Club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    return club


def test_a_batch_with_no_club_accepts_any_club(
    players: list[Player], event: Event, member: User
) -> None:
    club = _granting_club(event)
    batch = SubmissionBatch.objects.create(submitted_by_user=member)

    match = _record(
        players,
        event=event,
        club=club,
        submitted_by_user=member,
        submission_batch=batch,
    )

    assert match.submission_batch_id == batch.pk


def test_a_batch_with_the_same_club_is_accepted(
    players: list[Player], event: Event, member: User
) -> None:
    club = _granting_club(event)
    batch = SubmissionBatch.objects.create(submitted_by_user=member, club=club)

    match = _record(
        players,
        event=event,
        club=club,
        submitted_by_user=member,
        submission_batch=batch,
    )

    assert match.submission_batch_id == batch.pk


def test_a_club_only_batch_match_is_refused_before_the_batch_check(
    players: list[Player], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR 0021: a match with a Club and no Event is refused, so the batch
    # is never looked at.
    club: Club = ClubFactory.create()
    account: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=account)
    batch = SubmissionBatch.objects.create(submitted_by_user=account, club=club)

    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("reached the batch check")

    monkeypatch.setattr(services, "check_batch", fail)
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(players, club=club, submitted_by_user=account, submission_batch=batch)

    assert _counts() == before


# Refusals


def _assert_refused(players: list[Player], **overrides: Any) -> None:
    before = _counts()

    with pytest.raises(ValueError, match=re.escape(BATCH_REFUSED)) as error:
        _record(players, **overrides)

    assert str(error.value) == BATCH_REFUSED
    assert _counts() == before


def test_an_unsaved_batch_is_refused(
    players: list[Player], event: Event, member: User
) -> None:
    batch = SubmissionBatch(submitted_by_user=member, event=event)

    _assert_refused(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )


def test_another_accounts_batch_is_refused(
    players: list[Player], event: Event, member: User
) -> None:
    other: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=other)
    batch = SubmissionBatch.objects.create(submitted_by_user=other, event=event)

    _assert_refused(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )


def test_an_applications_batch_is_refused_for_an_account(
    players: list[Player],
    event: Event,
    member: User,
    application: PartnerApplication,
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_application=application)

    _assert_refused(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )


def test_an_accounts_batch_is_refused_for_an_application(
    players: list[Player],
    event: Event,
    member: User,
    application: PartnerApplication,
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_user=member)

    _assert_refused(
        players,
        event=event,
        submitted_by_application=application,
        submission_batch=batch,
    )


def test_another_applications_batch_is_refused(
    players: list[Player], event: Event, application: PartnerApplication
) -> None:
    other = approved_application(organizer=event.organizer)
    batch = SubmissionBatch.objects.create(submitted_by_application=other)

    _assert_refused(
        players,
        event=event,
        submitted_by_application=application,
        submission_batch=batch,
    )


def test_a_batch_for_another_event_is_refused(
    players: list[Player], event: Event, member: User
) -> None:
    other = EventFactory.create(organizer=event.organizer)
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=other)

    _assert_refused(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )


def test_a_batch_with_an_event_is_refused_for_a_match_without_one(
    players: list[Player], event: Event
) -> None:
    account = players[0].account
    batch = SubmissionBatch.objects.create(submitted_by_user=account, event=event)

    _assert_refused(players, submitted_by_user=account, submission_batch=batch)


def test_a_batch_for_another_club_is_refused(
    players: list[Player], event: Event, member: User
) -> None:
    club = _granting_club(event)
    other = _granting_club(event)
    batch = SubmissionBatch.objects.create(submitted_by_user=member, club=other)

    _assert_refused(
        players,
        event=event,
        club=club,
        submitted_by_user=member,
        submission_batch=batch,
    )


def test_the_batch_is_checked_before_validation(
    players: list[Player], event: Event, member: User
) -> None:
    other: User = UserFactory.create()
    batch = SubmissionBatch.objects.create(submitted_by_user=other)

    _assert_refused(
        players,
        event=event,
        submitted_by_user=member,
        submission_batch=batch,
        games=INVALID_GAMES,
    )


def test_a_refused_submitter_gets_the_permission_error_first(
    players: list[Player], event: Event
) -> None:
    outsider: User = UserFactory.create()
    other: User = UserFactory.create()
    batch = SubmissionBatch.objects.create(submitted_by_user=other)
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(
            players,
            event=event,
            submitted_by_user=outsider,
            submission_batch=batch,
            games=INVALID_GAMES,
        )

    assert _counts() == before


# After creation


def test_a_correction_is_not_in_the_batch(
    players: list[Player], event: Event, member: User
) -> None:
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    match = _record(
        players, event=event, submitted_by_user=member, submission_batch=batch
    )

    corrected = edit_match(
        match,
        actor=member,
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=((11, 3), (11, 5)),
        played_on=PLAYED_ON,
    )

    assert Match.objects.get(pk=corrected.pk).submission_batch_id is None
    assert list(batch.matches.all()) == [match]


@pytest.mark.parametrize("update_fields", [None, ["submission_batch"]])
def test_the_batch_link_cannot_change_later(
    players: list[Player],
    event: Event,
    member: User,
    update_fields: list[str] | None,
) -> None:
    match = _record(players, event=event, submitted_by_user=member)
    match.submission_batch = SubmissionBatch.objects.create(submitted_by_user=member)

    with pytest.raises(MatchImmutable):
        match.save(update_fields=update_fields)

    assert Match.objects.get(pk=match.pk).submission_batch_id is None


# A batch never confirms a pending match (#71, decision 2)


def _pending_self_submission(players: Sequence[Player]) -> Match:
    return _record(players, submitted_by_user=players[0].account)


def test_a_batch_exact_duplicate_of_a_pending_match_is_refused(
    players: list[Player], event: Event, member: User, signals: SignalLog
) -> None:
    pending = _pending_self_submission(players)
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    before = _counts()

    with pytest.raises(DuplicateMatchError) as caught:
        _record(players, event=event, submitted_by_user=member, submission_batch=batch)

    assert caught.value.kind == DuplicateKind.DUPLICATE_MATCH
    # The organizer did not submit it and plays in none of it.
    assert caught.value.existing_code is None
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    assert _counts() == before
    assert signals.confirmed == []


def test_an_application_batch_never_confirms_a_pending_match(
    players: list[Player],
    event: Event,
    application: PartnerApplication,
    signals: SignalLog,
) -> None:
    pending = _pending_self_submission(players)
    batch = SubmissionBatch.objects.create(submitted_by_application=application)
    before = _counts()

    with pytest.raises(DuplicateMatchError):
        _record(
            players,
            event=event,
            submitted_by_application=application,
            submission_batch=batch,
        )

    assert Match.objects.get(pk=pending.pk).status == MatchStatus.PENDING
    assert _counts() == before
    assert signals.confirmed == []


def test_without_a_batch_the_same_duplicate_still_confirms(
    players: list[Player], event: Event, member: User
) -> None:
    pending = _pending_self_submission(players)

    recorded = record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:],
        games=GAMES,
        played_on=PLAYED_ON,
        event=event,
        submitted_by_user=member,
    )

    assert recorded.created is False
    assert recorded.match.pk == pending.pk
    assert Match.objects.get(pk=pending.pk).status == MatchStatus.CONFIRMED
