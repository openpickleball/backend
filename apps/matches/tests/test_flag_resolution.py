"""A moderator resolves a match's open flags (#40). Made-up data only.

``resolve_flags`` closes every open flag that involves a match, as its
``match`` or its ``duplicate_of``. A resolved flag no longer holds either
match from counting, and each match that counts again sends ``release``.
"""

import pytest
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.matches.models import (
    FlagReason,
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import _linked, confirm_match, resolve_flags, void_match
from apps.players.models import Player

pytestmark = pytest.mark.django_db

REASON = "Made-up review reason."


def _near_duplicate(players: list[Player]) -> Match:
    """The same lineup and date as ``pending``, other scores: flagged."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=((11, 5), (11, 6)),
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match


def _confirm(match: Match, players: list[Player]) -> Match:
    confirm_match(match, actor=players[0].account)
    return confirm_match(match, actor=players[2].account)


@pytest.fixture
def pair(confirmed: Match, players: list[Player]) -> tuple[Match, Match]:
    """Two confirmed matches a possible duplicate flag holds."""
    later = _confirm(_near_duplicate(players), players)
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    assert not confirmed.is_counted()
    assert not later.is_counted()
    return confirmed, later


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def test_resolution_choices() -> None:
    assert {value for value in FlagResolution.values} == {"approved", "dismissed"}


def test_a_new_flag_is_open(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()

    assert flag.resolution == ""
    assert flag.resolved_at is None


def test_resolution_and_time_are_set_together(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()
    flag.resolution = FlagResolution.APPROVED

    with pytest.raises(IntegrityError), transaction.atomic():
        flag.save()


def test_time_without_resolution_is_refused(pair: tuple[Match, Match]) -> None:
    flag = MatchFlag.objects.get()
    flag.resolved_at = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        flag.save()


def test_counted_ignores_a_resolved_flag(pair: tuple[Match, Match]) -> None:
    earlier, later = pair
    flag = MatchFlag.objects.get()
    flag.resolution = FlagResolution.DISMISSED
    flag.resolved_at = timezone.now()
    flag.save()

    assert _counted() == {earlier.pk, later.pk}


@pytest.mark.parametrize("outcome", [FlagResolution.APPROVED, FlagResolution.DISMISSED])
def test_resolve_closes_the_flag_and_releases_both(
    pair: tuple[Match, Match],
    moderator: User,
    signals: SignalLog,
    outcome: FlagResolution,
) -> None:
    earlier, later = pair

    resolved = resolve_flags(later, actor=moderator, outcome=outcome, reason=REASON)

    assert resolved == 1
    flag = MatchFlag.objects.get()
    assert flag.resolution == outcome
    assert flag.resolved_at is not None
    assert _counted() == {earlier.pk, later.pk}
    assert signals.changes(earlier) == [MatchChange.RELEASE]
    assert signals.changes(later) == [MatchChange.RELEASE]
    assert signals.confirmed == []
    for match in (earlier, later):
        match.refresh_from_db()
        assert match.status == MatchStatus.CONFIRMED


def test_resolve_from_the_duplicate_of_side(
    pair: tuple[Match, Match], moderator: User
) -> None:
    earlier, later = pair

    assert (
        resolve_flags(
            earlier, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
        )
        == 1
    )
    assert _counted() == {earlier.pk, later.pk}


def test_resolve_records_one_event_per_flag_with_the_reason(
    pair: tuple[Match, Match], players: list[Player], moderator: User
) -> None:
    earlier, later = pair
    third = _confirm(
        record_match(
            format=MatchFormat.DOUBLES,
            team_a=players[:2],
            team_b=players[2:],
            games=((11, 2), (11, 3)),
            played_on=WORKFLOW_PLAYED_ON,
            submitted_by_user=players[0].account,
        ).match,
        players,
    )
    flags = MatchFlag.objects.filter(match=third)
    assert flags.count() == 2

    resolved = resolve_flags(
        earlier, actor=moderator, outcome=FlagResolution.DISMISSED, reason=REASON
    )

    # Only the flags that involve ``earlier``: later's and third's to it.
    assert resolved == 2
    events = AuditEvent.objects.filter(action=AuditAction.MATCH_FLAG_RESOLVE)
    assert events.count() == 2
    flag_type = ContentType.objects.get_for_model(MatchFlag)
    involved = MatchFlag.objects.filter(duplicate_of=earlier)
    assert {(e.target_content_type_id, e.target_object_id) for e in events} == {
        (flag_type.pk, str(flag.pk)) for flag in involved
    }
    for event in events:
        assert event.reason == REASON
        assert event.actor_user_id == moderator.pk
        assert event.after is not None
        assert event.after["resolution"] == FlagResolution.DISMISSED
    # The flag between later and third stays open, so both stay held.
    assert MatchFlag.objects.get(match=third, duplicate_of=later).resolved_at is None
    assert _counted() == {earlier.pk}


def test_resolve_with_no_open_flag_does_nothing(
    confirmed: Match, moderator: User, signals: SignalLog
) -> None:
    resolved = resolve_flags(
        confirmed, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    assert resolved == 0
    assert not AuditEvent.objects.filter(action=AuditAction.MATCH_FLAG_RESOLVE).exists()
    assert signals.changed == []


def test_resolving_twice_does_nothing_the_second_time(
    pair: tuple[Match, Match], moderator: User, signals: SignalLog
) -> None:
    _earlier, later = pair
    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )
    signals.changed.clear()

    again = resolve_flags(
        later, actor=moderator, outcome=FlagResolution.DISMISSED, reason=REASON
    )

    assert again == 0
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert AuditEvent.objects.filter(action=AuditAction.MATCH_FLAG_RESOLVE).count() == 1
    assert signals.changed == []


def test_a_pending_match_is_not_released(
    confirmed: Match, players: list[Player], moderator: User, signals: SignalLog
) -> None:
    later = _near_duplicate(players)

    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    assert signals.changes(confirmed) == [MatchChange.RELEASE]
    assert signals.changes(later) == []
    assert _counted() == {confirmed.pk}


def test_non_moderator_is_refused_before_anything(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    _earlier, later = pair
    staff = UserFactory.create(is_staff=True)

    for actor in (players[0].account, staff):
        with pytest.raises(MatchActionDenied):
            resolve_flags(
                later, actor=actor, outcome=FlagResolution.APPROVED, reason=REASON
            )
        # Refused before the outcome or reason is looked at.
        with pytest.raises(MatchActionDenied):
            resolve_flags(later, actor=actor, outcome="bogus", reason="")

    assert MatchFlag.objects.get().resolved_at is None
    assert signals.changed == []


def test_inactive_moderator_is_refused(
    pair: tuple[Match, Match], moderator: User
) -> None:
    _earlier, later = pair
    User.objects.filter(pk=moderator.pk).update(is_active=False)

    with pytest.raises(MatchActionDenied):
        resolve_flags(
            later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
        )


def test_unsaved_match_is_refused(moderator: User) -> None:
    with pytest.raises(MatchActionDenied):
        resolve_flags(
            MatchFactory.build(),
            actor=moderator,
            outcome=FlagResolution.APPROVED,
            reason=REASON,
        )


@pytest.mark.parametrize("reason", ["", "   \n"])
def test_a_reason_is_required(
    pair: tuple[Match, Match], moderator: User, reason: str
) -> None:
    _earlier, later = pair

    with pytest.raises(ValueError, match="reason"):
        resolve_flags(
            later, actor=moderator, outcome=FlagResolution.APPROVED, reason=reason
        )

    assert MatchFlag.objects.get().resolved_at is None


@pytest.mark.parametrize("outcome", ["", "confirmed", "voided", "open"])
def test_outcome_must_be_approved_or_dismissed(
    pair: tuple[Match, Match], moderator: User, outcome: str
) -> None:
    _earlier, later = pair

    with pytest.raises(ValueError, match="approved or dismissed"):
        resolve_flags(later, actor=moderator, outcome=outcome, reason=REASON)

    assert MatchFlag.objects.get().resolved_at is None


def test_linked_ignores_resolved_flags(
    pair: tuple[Match, Match], moderator: User
) -> None:
    earlier, later = pair
    assert [m.pk for m in _linked(later)] == [earlier.pk]

    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )

    assert _linked(later) == []
    assert _linked(earlier) == []


def test_voiding_after_resolution_sends_no_signal_for_the_other(
    pair: tuple[Match, Match], moderator: User, signals: SignalLog
) -> None:
    earlier, later = pair
    resolve_flags(
        later, actor=moderator, outcome=FlagResolution.APPROVED, reason=REASON
    )
    signals.changed.clear()

    void_match(later, actor=moderator, reason=REASON)

    assert signals.changes(later) == [MatchChange.VOID]
    assert signals.changes(earlier) == []
    assert _counted() == {earlier.pk}


def test_an_open_flag_still_holds_a_voided_partner_rule(
    pair: tuple[Match, Match],
) -> None:
    """Unchanged: a voided match stops holding the other (ADR 0013)."""
    earlier, later = pair
    raw_update(later, status=MatchStatus.VOIDED)

    assert _counted() == {earlier.pk}
    assert MatchFlag.objects.get().reason == FlagReason.POSSIBLE_DUPLICATE
