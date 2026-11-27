"""A bulk void never releases a hold on the moderator's own match (#326).

Made-up data only. Voiding a Match ends the holds its open
``possible_duplicate`` flags place on other Matches, so a moderator whose
own Player is in the correction chain of a Match an open flag links to
the target is refused on the target too, as for the target itself
(``moderates``, ADR 0029). The linked Match stays held, and no release is
sent.
"""

from datetime import date

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.tests.factories import OrganizerMemberFactory
from apps.events.models import Event
from apps.matches.models import (
    FlagReason,
    Match,
    MatchFormat,
    MatchStatus,
    SubmissionBatch,
)
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import SignalLog
from apps.matches.workflow import (
    BulkVoidResult,
    edit_match,
    void_submission_batch,
    void_submitter_matches,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

REASON = "Made-up reason: a compromised upload."
PLAYED_ON = date(2026, 3, 2)
GAMES = ((11, 7), (11, 9))
OTHER_GAMES = ((11, 5), (11, 3))


def _member(event: Event) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account)
    return account


def _moderator_account(account: User) -> User:
    account.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="matches", codename="moderate_match"
        )
    )
    return User.objects.get(pk=account.pk)


def _held_pair(
    event: Event, *, own: Player, rivals: list[Player], uploader: User
) -> tuple[Match, Match, SubmissionBatch]:
    """Match B, whose correction chain holds ``own``, and Match A, in a
    batch ``uploader`` sent, with an open flag linking A to B.

    B's original had ``own`` on team A; its correction has the same teams
    as A, so A's record flags it. ``own`` is in neither A's chain nor its
    teams.
    """
    submitter = _member(event)
    original = record_match(
        format=MatchFormat.SINGLES,
        team_a=[own],
        team_b=[rivals[1]],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=submitter,
        event=event,
    ).match
    held = edit_match(
        original,
        actor=submitter,
        format=MatchFormat.SINGLES,
        team_a=[rivals[0]],
        team_b=[rivals[1]],
        games=GAMES,
        played_on=PLAYED_ON,
    )
    batch = SubmissionBatch.objects.create(submitted_by_user=uploader, event=event)
    target = record_match(
        format=MatchFormat.SINGLES,
        team_a=[rivals[0]],
        team_b=[rivals[1]],
        games=OTHER_GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=uploader,
        event=event,
        submission_batch=batch,
    ).match
    assert target.flags.filter(
        reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=held
    ).exists()
    assert held.status == MatchStatus.CONFIRMED
    assert not held.is_counted()
    return held, target, batch


def test_a_batch_void_refuses_a_match_whose_void_would_release_the_moderators_own(
    event: Event, signals: SignalLog
) -> None:
    own = PlayerFactory.create()
    rivals = [PlayerFactory.create() for _ in range(2)]
    uploader = _member(event)
    held, target, batch = _held_pair(event, own=own, rivals=rivals, uploader=uploader)
    moderator = _moderator_account(own.account)
    signals.changed.clear()

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=0, skipped=0, refused=1)
    assert Match.objects.get(pk=target.pk).status == MatchStatus.CONFIRMED
    assert not Match.objects.get(pk=held.pk).is_counted()
    assert signals.changes(held) == []
    assert signals.changes(target) == []


def test_a_submitter_void_refuses_a_match_whose_void_would_release_the_moderators_own(
    event: Event, signals: SignalLog
) -> None:
    own = PlayerFactory.create()
    rivals = [PlayerFactory.create() for _ in range(2)]
    uploader = _member(event)
    held, target, _batch = _held_pair(event, own=own, rivals=rivals, uploader=uploader)
    moderator = _moderator_account(own.account)
    signals.changed.clear()

    result = void_submitter_matches(
        uploader, recorded_since=date(2000, 1, 1), actor=moderator, reason=REASON
    )

    assert result == BulkVoidResult(voided=0, skipped=0, refused=1)
    assert Match.objects.get(pk=target.pk).status == MatchStatus.CONFIRMED
    assert not Match.objects.get(pk=held.pk).is_counted()
    assert signals.changes(held) == []
    assert signals.changes(target) == []


def test_another_moderator_voids_it_and_the_linked_match_counts_again(
    event: Event, moderator: User, signals: SignalLog
) -> None:
    own = PlayerFactory.create()
    rivals = [PlayerFactory.create() for _ in range(2)]
    uploader = _member(event)
    held, target, batch = _held_pair(event, own=own, rivals=rivals, uploader=uploader)
    signals.changed.clear()

    result = void_submission_batch(batch, actor=moderator, reason=REASON)

    assert result == BulkVoidResult(voided=1, skipped=0, refused=0)
    assert Match.objects.get(pk=target.pk).status == MatchStatus.VOIDED
    assert Match.objects.get(pk=held.pk).is_counted()
    assert signals.changes(held) == [MatchChange.RELEASE]
