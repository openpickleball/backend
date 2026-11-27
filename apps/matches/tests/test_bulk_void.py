"""A moderator voids an upload's matches in bulk (#205). Made-up data only.

``void_submission_batch`` voids every match of one Submission Batch, found
by the batch link, and ``void_submitter_matches`` every match one
submitter recorded on or after a date. Each match is voided in its own
transaction, with its own ``match.void`` Audit Event and signals, and each
run records one ``match.bulk_void`` event. Only a moderator may run them
(the bulk void is a moderator's only match action, #273), a match whose
correction chain holds the moderator's own Player counts as refused, and
a rerun voids nothing.
"""

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser, Permission
from django.contrib.contenttypes.models import ContentType

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    Match,
    MatchFormat,
    MatchStatus,
    SubmissionBatch,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import SignalLog
from apps.matches.tests.factories import raw_update, void_for_test
from apps.matches.workflow import (
    BulkVoidResult,
    dispute_match,
    submission_batch_matches,
    submitter_matches,
    void_submission_batch,
    void_submitter_matches,
)
from apps.partners.models import PartnerApplication
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason: a compromised upload."
FIRST_DAY = date(2026, 1, 1)
GAMES = ((11, 7), (11, 9))


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _record(
    players: list[Player],
    day: int,
    event: Event,
    *,
    user: User | None = None,
    application: PartnerApplication | None = None,
    batch: SubmissionBatch | None = None,
) -> Match:
    return record_match(
        format=MatchFormat.SINGLES,
        team_a=players[:1],
        team_b=players[1:2],
        games=GAMES,
        played_on=FIRST_DAY + timedelta(days=day),
        event=event,
        submitted_by_user=user,
        submitted_by_application=application,
        submission_batch=batch,
    ).match


def _batch_of(
    count: int,
    players: list[Player],
    event: Event,
    member: User,
    *,
    first: int = 0,
) -> tuple[SubmissionBatch, list[Match]]:
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    matches = [
        _record(players, first + day, event, user=member, batch=batch)
        for day in range(count)
    ]
    return batch, matches


def _statuses(matches: list[Match]) -> list[str]:
    by_pk = dict(
        Match.objects.filter(pk__in=[m.pk for m in matches]).values_list("pk", "status")
    )
    return [by_pk[m.pk] for m in matches]


def _events(action: AuditAction) -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=action).order_by("pk"))


def _target_ids(action: AuditAction) -> list[int]:
    return sorted(int(e.target_object_id) for e in _events(action))


def _inactive_moderator() -> User:
    account: User = UserFactory.create(is_active=False)
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return account


# One batch


def test_a_moderator_voids_all_100_matches_of_a_batch(
    players: list[Player],
    event: Event,
    member: User,
    moderator: User,
    signals: SignalLog,
) -> None:
    batch, matches = _batch_of(100, players, event, member)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=100, skipped=0, refused=0)
    assert set(_statuses(matches)) == {MatchStatus.VOIDED}
    voids = _events(AuditAction.MATCH_VOID)
    assert sorted(int(e.target_object_id) for e in voids) == sorted(
        m.pk for m in matches
    )
    assert {e.actor_user_id for e in voids} == {moderator.pk}
    assert {e.reason for e in voids} == {REASON}
    for match in matches:
        assert signals.changes(match) == [MatchChange.VOID]
    [bulk] = _events(AuditAction.MATCH_BULK_VOID)
    assert bulk.target_content_type == ContentType.objects.get_for_model(
        SubmissionBatch
    )
    assert bulk.target_object_id == str(batch.pk)
    assert bulk.actor_user_id == moderator.pk
    assert bulk.reason == REASON
    assert bulk.after == {
        "scope": "batch",
        "recorded_since": None,
        "voided": 100,
        "skipped": 0,
        "refused": 0,
    }


def test_a_second_run_voids_nothing_and_skips_every_match(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    batch, _matches = _batch_of(5, players, event, member)
    void_submission_batch(batch, actor=moderator, reason=REASON)

    again = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert again == BulkVoidResult(voided=0, skipped=5, refused=0)
    assert len(_events(AuditAction.MATCH_VOID)) == 5
    bulk = _events(AuditAction.MATCH_BULK_VOID)
    assert len(bulk) == 2
    assert bulk[1].after == {
        "scope": "batch",
        "recorded_since": None,
        "voided": 0,
        "skipped": 5,
        "refused": 0,
    }


def test_a_match_already_voided_is_skipped(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    batch, matches = _batch_of(3, players, event, member)
    # Voided first in setup (void_for_test), outside the bulk void.
    void_for_test(matches[1], actor=member)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=2, skipped=1, refused=0)
    assert set(_statuses(matches)) == {MatchStatus.VOIDED}
    assert len(_events(AuditAction.MATCH_VOID)) == 3


def test_a_pending_and_a_disputed_match_are_voided(
    players: list[Player], moderator: User
) -> None:
    account = players[0].account
    batch = SubmissionBatch.objects.create(submitted_by_user=account)
    matches = [
        record_match(
            format=MatchFormat.SINGLES,
            team_a=players[:1],
            team_b=players[1:2],
            games=GAMES,
            played_on=FIRST_DAY + timedelta(days=day),
            submitted_by_user=account,
            submission_batch=batch,
        ).match
        for day in range(2)
    ]
    dispute_match(matches[1], actor=players[1].account)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=2, skipped=0, refused=0)
    assert set(_statuses(matches)) == {MatchStatus.VOIDED}


def test_matches_outside_the_batch_stay_untouched(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    batch, inside = _batch_of(3, players, event, member)
    _other_batch, other = _batch_of(2, players, event, member, first=10)
    single = [_record(players, 20 + day, event, user=member) for day in range(2)]

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=3, skipped=0, refused=0)
    assert set(_statuses(inside)) == {MatchStatus.VOIDED}
    assert set(_statuses(other + single)) == {MatchStatus.CONFIRMED}


def test_the_batch_helper_lists_the_batch_matches_in_order(
    players: list[Player], event: Event, member: User
) -> None:
    batch, matches = _batch_of(3, players, event, member)
    _batch_of(1, players, event, member, first=10)

    assert list(submission_batch_matches(batch)) == sorted(matches, key=lambda m: m.pk)


def test_a_moderator_who_played_in_one_match_is_refused_for_it(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    own = PlayerFactory.create(account=moderator)
    batch, matches = _batch_of(3, players, event, member)
    played = _record([own, players[1]], 30, event, user=member, batch=batch)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=3, skipped=0, refused=1)
    assert set(_statuses(matches)) == {MatchStatus.VOIDED}
    assert Match.objects.get(pk=played.pk).status == MatchStatus.CONFIRMED
    [bulk] = _events(AuditAction.MATCH_BULK_VOID)
    assert bulk.after is not None
    assert bulk.after["refused"] == 1


# Who may run it


@pytest.mark.parametrize(
    "make_actor",
    [
        lambda: UserFactory.create(),
        lambda: UserFactory.create(is_staff=True),
        _inactive_moderator,
        lambda: UserFactory.build(),
        lambda: AnonymousUser(),
        lambda: approved_application(),
    ],
    ids=[
        "account",
        "staff_without_permission",
        "inactive_moderator",
        "unsaved_account",
        "anonymous",
        "application",
    ],
)
def test_anyone_but_a_moderator_is_refused_and_nothing_changes(
    players: list[Player], event: Event, member: User, make_actor: Callable[[], Any]
) -> None:
    batch, matches = _batch_of(3, players, event, member)
    actor = make_actor()
    before = AuditEvent.objects.count()

    with pytest.raises(MatchActionDenied):
        void_submission_batch(batch, actor=actor, reason=REASON)
    with pytest.raises(MatchActionDenied):
        void_submitter_matches(
            member, recorded_since=FIRST_DAY, actor=actor, reason=REASON
        )

    assert set(_statuses(matches)) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


def test_the_permission_is_checked_before_the_reason(
    players: list[Player], event: Event, member: User
) -> None:
    batch, _matches = _batch_of(1, players, event, member)
    outsider: User = UserFactory.create()

    with pytest.raises(MatchActionDenied):
        void_submission_batch(batch, actor=outsider, reason="")
    with pytest.raises(MatchActionDenied):
        void_submission_batch(
            SubmissionBatch(submitted_by_user=member), actor=outsider, reason=""
        )


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_is_refused_and_nothing_changes(
    players: list[Player],
    event: Event,
    member: User,
    moderator: User,
    reason: str,
) -> None:
    batch, matches = _batch_of(2, players, event, member)
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="reason"):
        void_submission_batch(batch, actor=moderator, reason=reason)
    with pytest.raises(ValueError, match="reason"):
        void_submitter_matches(
            member, recorded_since=FIRST_DAY, actor=moderator, reason=reason
        )

    assert set(_statuses(matches)) == {MatchStatus.CONFIRMED}
    assert AuditEvent.objects.count() == before


def test_an_unsaved_batch_is_refused(member: User, moderator: User) -> None:
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match=re.escape("submission batch")):
        void_submission_batch(
            SubmissionBatch(submitted_by_user=member), actor=moderator, reason=REASON
        )

    assert AuditEvent.objects.count() == before


def test_an_unsaved_submitter_is_refused(moderator: User) -> None:
    before = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="submitter"):
        void_submitter_matches(
            UserFactory.build(),
            recorded_since=FIRST_DAY,
            actor=moderator,
            reason=REASON,
        )

    assert AuditEvent.objects.count() == before


# One submitter since a date


def test_a_submitters_matches_since_a_date_are_voided(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    since = date(2026, 11, 20)
    before_day = _record(players, 0, event, user=member)
    at_midnight = _record(players, 1, event, user=member)
    later = _record(players, 2, event, user=member)
    raw_update(
        before_day,
        created_at=datetime(2026, 11, 19, 23, 59, 59, 999999, tzinfo=UTC),
    )
    raw_update(at_midnight, created_at=datetime(2026, 11, 20, tzinfo=UTC))
    raw_update(later, created_at=datetime(2026, 11, 22, 8, 30, tzinfo=UTC))
    other: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=other)
    someone_else = _record(players, 3, event, user=other)
    raw_update(someone_else, created_at=datetime(2026, 11, 22, tzinfo=UTC))

    result = void_submitter_matches(
        member, recorded_since=since, actor=moderator, reason=REASON
    )

    assert result == BulkVoidResult(voided=2, skipped=0, refused=0)
    assert _statuses([before_day, at_midnight, later, someone_else]) == [
        MatchStatus.CONFIRMED,
        MatchStatus.VOIDED,
        MatchStatus.VOIDED,
        MatchStatus.CONFIRMED,
    ]
    assert list(submitter_matches(member, since)) == [at_midnight, later]
    [bulk] = _events(AuditAction.MATCH_BULK_VOID)
    assert bulk.target_content_type == ContentType.objects.get_for_model(User)
    assert bulk.target_object_id == str(member.pk)
    assert bulk.actor_user_id == moderator.pk
    assert bulk.reason == REASON
    assert bulk.after == {
        "scope": "submitter",
        "recorded_since": "2026-11-20",
        "voided": 2,
        "skipped": 0,
        "refused": 0,
    }
    assert member.email not in str(bulk.after)


def test_an_applications_matches_since_a_date_are_voided(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    application = approved_application(organizer=event.organizer)
    sent = [_record(players, day, event, application=application) for day in range(3)]
    by_account = _record(players, 10, event, user=member)

    result = void_submitter_matches(
        application, recorded_since=FIRST_DAY, actor=moderator, reason=REASON
    )

    assert result == BulkVoidResult(voided=3, skipped=0, refused=0)
    assert set(_statuses(sent)) == {MatchStatus.VOIDED}
    assert Match.objects.get(pk=by_account.pk).status == MatchStatus.CONFIRMED
    [bulk] = _events(AuditAction.MATCH_BULK_VOID)
    assert bulk.target_content_type == ContentType.objects.get_for_model(
        PartnerApplication
    )
    assert bulk.target_object_id == str(application.pk)
    assert bulk.after is not None
    assert bulk.after["scope"] == "submitter"
    assert _target_ids(AuditAction.MATCH_VOID) == sorted(m.pk for m in sent)


def test_a_second_submitter_run_skips_every_match(
    players: list[Player], event: Event, member: User, moderator: User
) -> None:
    sent = [_record(players, day, event, user=member) for day in range(3)]
    void_submitter_matches(
        member, recorded_since=FIRST_DAY, actor=moderator, reason=REASON
    )

    again = void_submitter_matches(
        member, recorded_since=FIRST_DAY, actor=moderator, reason=REASON
    )

    assert again == BulkVoidResult(voided=0, skipped=3, refused=0)
    assert set(_statuses(sent)) == {MatchStatus.VOIDED}
    assert len(_events(AuditAction.MATCH_BULK_VOID)) == 2
