"""How the bulk void voids each match as moderator (#273). Made-up data only.

The bulk void is a moderator's only match action. Each match is voided in
its own transaction through ``workflow._void_as_moderator``, which takes
the match's lineup advisory lock before its row lock, as every other
status change does, and checks ``moderates`` again for each match, so an
actor who stops being a moderator partway through a run gets the
remaining matches counted as refused.
"""

from typing import Any

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches import workflow
from apps.matches.models import Match, MatchStatus, SubmissionBatch
from apps.matches.services import stored_lineup_lock_key
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.tests.test_bulk_void import REASON, _batch_of, _statuses
from apps.matches.tests.test_fix_duplicate_race_and_edit_locks import (
    held_advisory_keys,
)
from apps.matches.workflow import BulkVoidResult, void_submission_batch
from apps.players.models import Player

pytestmark = pytest.mark.django_db


@pytest.fixture
def member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def test_each_match_takes_its_lineup_lock_before_its_row_lock(
    monkeypatch: pytest.MonkeyPatch,
    players: list[Player],
    event: Event,
    member: User,
    moderator: User,
) -> None:
    # Built by the factory, not record_match, so no lineup lock is held
    # before the run.
    batch = SubmissionBatch.objects.create(submitted_by_user=member, event=event)
    matches = [
        MatchFactory.create(
            event=event, submitted_by_user=member, submission_batch=batch
        )
        for _ in range(3)
    ]
    raw_update(matches[0], status=MatchStatus.CONFIRMED)
    raw_update(matches[1], status=MatchStatus.CONFIRMED)
    raw_update(matches[2], status=MatchStatus.CONFIRMED)
    assert held_advisory_keys() == set()
    held_at_row_lock: dict[int, set[int]] = {}
    lock_row = workflow._lock_row

    def recording_lock_row(match: Match) -> Match:
        held_at_row_lock[match.pk] = held_advisory_keys()
        return lock_row(match)

    monkeypatch.setattr(workflow, "_lock_row", recording_lock_row)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=3, skipped=0, refused=0)
    assert set(held_at_row_lock) == {m.pk for m in matches}
    for match in matches:
        assert stored_lineup_lock_key(match) in held_at_row_lock[match.pk]


def test_losing_the_permission_partway_refuses_the_remaining_matches(
    monkeypatch: pytest.MonkeyPatch,
    players: list[Player],
    event: Event,
    member: User,
    moderator: User,
) -> None:
    batch, matches = _batch_of(3, players, event, member)
    void = workflow._void

    def void_then_lose_permission(*args: Any, **kwargs: Any) -> None:
        void(*args, **kwargs)
        moderator.user_permissions.remove(
            Permission.objects.get(
                content_type__app_label="matches", codename="moderate_match"
            )
        )

    monkeypatch.setattr(workflow, "_void", void_then_lose_permission)

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=1, skipped=0, refused=2)
    assert _statuses(matches) == [
        MatchStatus.VOIDED,
        MatchStatus.CONFIRMED,
        MatchStatus.CONFIRMED,
    ]
