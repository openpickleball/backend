"""Players and organizers release a possible duplicate hold (#270, ADR 0034).

``vouch_for_match`` records that a held match is not a duplicate. Each
match of the flag is vouched for at its own level: an Event match by its
Event's verified Organizer, a self-reported match by one player of each
team. Once both matches are vouched for, the flag is resolved as approved
and each match that counts again sends ``release``. Made-up data only.
"""

import pytest
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    FlagReason,
    FlagResolution,
    Match,
    MatchFlag,
    MatchFormat,
    MatchStatus,
    MatchVouch,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import WORKFLOW_PLAYED_ON, SignalLog
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import (
    MatchStateError,
    confirm_match,
    vouch_for_match,
    vouchable_holds,
    vouched_at_own_level,
)
from apps.partners.tests.factories import approved_application
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db


def _duplicate_flags() -> "QuerySet[MatchFlag]":
    """Possible duplicate flags only. Event matches also carry signal-only
    unusual submission flags (#206), which hold nothing."""
    return MatchFlag.objects.filter(reason=FlagReason.POSSIBLE_DUPLICATE)


def _self_reported(
    players: list[Player], games: tuple[tuple[int, int], ...], confirm: bool = True
) -> Match:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    if confirm:
        confirm_match(match, actor=players[0].account)
        confirm_match(match, actor=players[2].account)
    return match


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _event_match(
    players: list[Player],
    event: Event,
    games: tuple[tuple[int, int], ...],
    **kwargs: object,
) -> Match:
    if "submitted_by_application" not in kwargs:
        kwargs.setdefault("submitted_by_user", _member(event))
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=games,
        played_on=WORKFLOW_PLAYED_ON,
        event=event,
        **kwargs,  # type: ignore[arg-type]
    ).match


@pytest.fixture
def pair(confirmed: Match, players: list[Player]) -> tuple[Match, Match]:
    """Two confirmed self-reported matches a possible duplicate flag holds."""
    later = _self_reported(players, ((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=confirmed).exists()
    assert not confirmed.is_counted()
    assert not later.is_counted()
    return confirmed, later


def _counted() -> set[int]:
    return set(Match.objects.counted().values_list("pk", flat=True))


def _vouch_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.MATCH_VOUCH))


def _resolve_events() -> list[AuditEvent]:
    return list(AuditEvent.objects.filter(action=AuditAction.MATCH_FLAG_RESOLVE))


def _vouch_both_teams(match: Match, players: list[Player]) -> None:
    vouch_for_match(match, actor=players[0].account)
    vouch_for_match(match, actor=players[3].account)


# Two self-reported matches (AC 1).


def test_both_teams_on_both_matches_resolve_the_flag(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    earlier, later = pair
    vouch_for_match(earlier, actor=players[0].account)
    vouch_for_match(earlier, actor=players[2].account)
    vouch_for_match(later, actor=players[1].account)
    assert MatchFlag.objects.get().resolved_at is None
    assert signals.changed == []

    vouch_for_match(later, actor=players[3].account)

    flag = MatchFlag.objects.get()
    assert flag.resolution == FlagResolution.APPROVED
    assert flag.resolved_at is not None
    assert _counted() == {earlier.pk, later.pk}
    assert signals.changes(earlier) == [MatchChange.RELEASE]
    assert signals.changes(later) == [MatchChange.RELEASE]
    [event] = _resolve_events()
    assert event.target_object_id == str(flag.pk)
    assert event.after is not None
    assert event.after["rule"] == "vouched"
    assert event.after["resolution"] == FlagResolution.APPROVED
    assert event.reason == ""
    assert event.actor_user_id == players[3].account_id


def test_one_team_on_both_matches_keeps_the_hold(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    for match in pair:
        vouch_for_match(match, actor=players[0].account)
        vouch_for_match(match, actor=players[1].account)

    assert MatchFlag.objects.get().resolved_at is None
    assert _counted() == set()
    assert signals.changed == []
    assert _resolve_events() == []


def test_both_teams_on_one_match_keep_the_hold(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    _vouch_both_teams(earlier, players)

    assert MatchFlag.objects.get().resolved_at is None
    assert _counted() == set()


def test_a_pending_match_may_be_vouched_for_but_does_not_count(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    later = _self_reported(players, ((11, 5), (11, 6)), confirm=False)
    assert later.status == MatchStatus.PENDING

    _vouch_both_teams(confirmed, players)
    _vouch_both_teams(later, players)

    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert _counted() == {confirmed.pk}
    # The near duplicate held it when it was recorded.
    assert signals.changes(confirmed) == [MatchChange.HOLD, MatchChange.RELEASE]
    assert signals.changes(later) == []


# An Event match and a self-reported match (AC 2).


@pytest.fixture
def mixed(players: list[Player], event: Event) -> tuple[Match, Match]:
    """An Event match and a later self-reported near duplicate, both confirmed."""
    earlier = _event_match(players, event, ((11, 7), (11, 9)))
    later = _self_reported(players, ((11, 5), (11, 6)))
    assert MatchFlag.objects.filter(match=later, duplicate_of=earlier).exists()
    return earlier, later


def test_organizer_and_one_team_keep_the_hold(
    mixed: tuple[Match, Match], players: list[Player], event: Event
) -> None:
    earlier, later = mixed
    vouch_for_match(earlier, actor=_member(event))
    vouch_for_match(later, actor=players[0].account)
    vouch_for_match(later, actor=players[1].account)

    assert _duplicate_flags().get().resolved_at is None
    assert _counted() == set()


def test_organizer_and_both_teams_resolve_the_flag(
    mixed: tuple[Match, Match], players: list[Player], event: Event, signals: SignalLog
) -> None:
    earlier, later = mixed
    _vouch_both_teams(later, players)
    assert _duplicate_flags().get().resolved_at is None

    vouch_for_match(earlier, actor=_member(event))

    assert _duplicate_flags().get().resolution == FlagResolution.APPROVED
    assert _counted() == {earlier.pk, later.pk}
    assert signals.changes(earlier) == [MatchChange.RELEASE]
    assert signals.changes(later) == [MatchChange.RELEASE]


def test_the_submitting_application_vouches_for_its_event_match(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    earlier = _event_match(
        players, event, ((11, 7), (11, 9)), submitted_by_application=application
    )
    later = _self_reported(players, ((11, 5), (11, 6)))
    _vouch_both_teams(later, players)

    vouch_for_match(earlier, actor=application)

    row = MatchVouch.objects.get(match=earlier)
    assert row.application_id == application.pk
    assert row.account_id is None
    event_row = AuditEvent.objects.get(
        action=AuditAction.MATCH_VOUCH, target_object_id=str(row.pk)
    )
    assert event_row.actor_application_id == application.pk
    assert event_row.actor_user_id is None
    assert _duplicate_flags().get().resolution == FlagResolution.APPROVED


# Vouch rows and Audit Events (AC 4).


def test_each_vouch_writes_one_row_and_one_audit_event(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    account = players[0].account

    vouch_for_match(earlier, actor=account)

    row = MatchVouch.objects.get()
    flag = MatchFlag.objects.get()
    assert (row.flag_id, row.match_id, row.account_id) == (
        flag.pk,
        earlier.pk,
        account.pk,
    )
    [event] = _vouch_events()
    assert event.target_object_id == str(row.pk)
    assert event.actor_user_id == account.pk
    assert event.after == {
        "flag": flag.pk,
        "match": earlier.public_code,
        "level": "player",
    }
    assert event.reason == ""


def test_a_repeat_vouch_writes_nothing(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    vouch_for_match(earlier, actor=players[0].account)

    returned = vouch_for_match(earlier, actor=players[0].account)

    assert returned.pk == earlier.pk
    assert MatchVouch.objects.count() == 1
    assert len(_vouch_events()) == 1


def test_a_match_in_two_flags_is_vouched_for_on_both(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    earlier, later = pair
    third = _self_reported(players, ((11, 2), (11, 3)))
    assert MatchFlag.objects.count() == 3

    vouch_for_match(earlier, actor=players[0].account)

    assert MatchVouch.objects.filter(match=earlier).count() == 2
    assert len(_vouch_events()) == 2
    # Clearing the earlier-later flag still leaves both held by the flags
    # with the third match.
    vouch_for_match(earlier, actor=players[2].account)
    _vouch_both_teams(later, players)
    assert MatchFlag.objects.filter(resolved_at__isnull=False).count() == 1
    assert _counted() == set()
    assert signals.changed == []

    _vouch_both_teams(third, players)

    assert not MatchFlag.objects.filter(resolved_at__isnull=True).exists()
    assert _counted() == {earlier.pk, later.pk, third.pk}


def test_a_flag_opened_after_a_vouch_needs_its_own_vouch(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair
    _vouch_both_teams(earlier, players)
    third = _self_reported(players, ((11, 2), (11, 3)))
    _vouch_both_teams(later, players)
    _vouch_both_teams(third, players)

    open_flags = MatchFlag.objects.filter(resolved_at__isnull=True)
    assert set(open_flags.values_list("match_id", "duplicate_of_id")) == {
        (third.pk, earlier.pk)
    }
    assert _counted() == {later.pk}


# State refusals (AC 4, 5).


def test_a_voided_match_is_refused(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    raw_update(earlier, status=MatchStatus.VOIDED)

    with pytest.raises(MatchStateError):
        vouch_for_match(earlier, actor=players[0].account)

    assert not MatchVouch.objects.exists()


def test_a_match_with_no_hold_is_refused(
    confirmed: Match, players: list[Player]
) -> None:
    with pytest.raises(MatchStateError):
        vouch_for_match(confirmed, actor=players[0].account)

    assert not MatchVouch.objects.exists()
    assert _vouch_events() == []


def test_voiding_the_other_match_still_ends_the_hold(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair
    raw_update(later, status=MatchStatus.VOIDED)

    assert _counted() == {earlier.pk}
    with pytest.raises(MatchStateError):
        vouch_for_match(earlier, actor=players[0].account)


def test_a_resolved_flag_is_no_hold(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair
    _vouch_both_teams(earlier, players)
    _vouch_both_teams(later, players)

    with pytest.raises(MatchStateError):
        vouch_for_match(earlier, actor=players[1].account)


# Refused actors (AC 3).


def test_a_non_participant_is_refused_before_anything_is_read(
    pair: tuple[Match, Match],
) -> None:
    earlier, _later = pair

    with pytest.raises(MatchActionDenied):
        vouch_for_match(earlier, actor=PlayerFactory.create().account)

    assert not MatchVouch.objects.exists()


def test_a_refused_actor_on_a_voided_match_gets_denied_not_state(
    pair: tuple[Match, Match],
) -> None:
    earlier, _later = pair
    raw_update(earlier, status=MatchStatus.VOIDED)

    with pytest.raises(MatchActionDenied):
        vouch_for_match(earlier, actor=PlayerFactory.create().account)


def test_a_participant_of_an_event_match_is_refused(
    mixed: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = mixed

    with pytest.raises(MatchActionDenied):
        vouch_for_match(earlier, actor=players[0].account)


def test_a_member_of_another_organizer_is_refused(
    mixed: tuple[Match, Match],
) -> None:
    earlier, _later = mixed
    other = EventFactory.create(organizer=verified_organizer())

    with pytest.raises(MatchActionDenied):
        vouch_for_match(earlier, actor=_member(other))


def test_a_moderator_who_is_not_a_member_is_refused(
    mixed: tuple[Match, Match], moderator: User
) -> None:
    earlier, later = mixed

    for match in (earlier, later):
        with pytest.raises(MatchActionDenied):
            vouch_for_match(match, actor=moderator)


def test_an_unsaved_match_is_refused(players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        vouch_for_match(Match(), actor=players[0].account)


# What the list and the viewer's own level read.


def test_vouchable_holds_lists_what_the_account_may_vouch_on(
    mixed: tuple[Match, Match], players: list[Player], event: Event
) -> None:
    earlier, later = mixed
    member = _member(event)
    outsider = PlayerFactory.create().account

    assert set(vouchable_holds(players[0].account)) == {later}
    assert set(vouchable_holds(member)) == {earlier}
    assert set(vouchable_holds(_member(event, players[1].account))) == {later}
    assert list(vouchable_holds(outsider)) == []


def test_vouchable_holds_skips_voided_and_released_matches(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, later = pair
    account = players[0].account
    assert set(vouchable_holds(account)) == {earlier, later}

    raw_update(later, status=MatchStatus.VOIDED)

    assert list(vouchable_holds(account)) == []


def test_vouched_at_own_level_reads_the_viewers_team(
    pair: tuple[Match, Match], players: list[Player]
) -> None:
    earlier, _later = pair
    vouch_for_match(earlier, actor=players[0].account)

    assert vouched_at_own_level(earlier, players[0].account)
    assert vouched_at_own_level(earlier, players[1].account)
    assert not vouched_at_own_level(earlier, players[2].account)


def test_vouched_at_own_level_for_an_organizer(
    mixed: tuple[Match, Match], event: Event
) -> None:
    earlier, _later = mixed
    member = _member(event)
    assert not vouched_at_own_level(earlier, member)

    vouch_for_match(earlier, actor=_member(event))

    assert vouched_at_own_level(earlier, member)


def test_a_legacy_club_only_match_keeps_its_hold(
    confirmed: Match, players: list[Player]
) -> None:
    club_only: Match = MatchFactory.create(
        club=ClubFactory.create(), participants=False
    )
    MatchFlag.objects.create(
        match=confirmed, duplicate_of=club_only, reason=FlagReason.POSSIBLE_DUPLICATE
    )

    _vouch_both_teams(confirmed, players)

    assert MatchVouch.objects.filter(match=confirmed).count() == 2
    assert MatchFlag.objects.get().resolved_at is None
    assert not confirmed.is_counted()


def test_vouched_at_own_level_is_false_with_no_hold(
    confirmed: Match, players: list[Player]
) -> None:
    assert not vouched_at_own_level(confirmed, players[0].account)


def test_any_vouch_resolves_a_flag_already_vouched_for_on_both_sides(
    pair: tuple[Match, Match], players: list[Player], signals: SignalLog
) -> None:
    earlier, later = pair
    flag = MatchFlag.objects.get()
    # Rows written outside the service, so no vouch completed the flag.
    for match, voucher in (
        (earlier, players[0]),
        (earlier, players[2]),
        (later, players[1]),
        (later, players[3]),
    ):
        MatchVouch.objects.create(flag=flag, match=match, account=voucher.account)

    vouch_for_match(earlier, actor=players[0].account)

    assert MatchVouch.objects.count() == 4
    assert _vouch_events() == []
    assert MatchFlag.objects.get().resolution == FlagResolution.APPROVED
    assert _counted() == {earlier.pk, later.pk}
    assert signals.changes(earlier) == [MatchChange.RELEASE]
