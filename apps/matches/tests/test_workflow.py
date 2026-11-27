"""The match confirmation workflow: confirm, dispute, resolve, edit (#26).

Resolving a dispute and editing are the Event's verified Organizer's (or
the submitting application's); a moderator does neither (#273). No
service voids a single match on its own (#327).

Every transition writes exactly one Audit Event and sends its signal
inside the transaction. A refused or failed action writes nothing and
sends nothing. Made-up data only.
"""

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    ConfirmationDecision,
    FlagReason,
    Match,
    MatchConfirmation,
    MatchFlag,
    MatchFormat,
    MatchStatus,
    Team,
)
from apps.matches.permissions import MatchActionDenied
from apps.matches.services import (
    DuplicateMatchError,
    MatchValidationError,
    record_match,
)
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import SignalLog
from apps.matches.tests.factories import MatchFactory, raw_update, void_for_test
from apps.matches.workflow import (
    MatchStateError,
    confirm_match,
    dispute_match,
    edit_match,
    resolve_dispute,
)
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (9, 11), (11, 4))
OTHER_GAMES = ((11, 5), (11, 8))
REASON = "Made-up reason for a test."


def _member(event: Event, role: str = MemberRole.MATCH_MANAGER) -> User:
    account: User = UserFactory.create()
    OrganizerMemberFactory.create(organizer=event.organizer, account=account, role=role)
    return account


def _audits(match: Match, action: AuditAction | None = None) -> list[AuditEvent]:
    events = AuditEvent.objects.filter(target_object_id=str(match.pk)).exclude(
        action=AuditAction.ORGANIZER_VERIFY
    )
    if action is not None:
        events = events.filter(action=action)
    return list(events.order_by("created_at", "id"))


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


# Confirmation (AC 1, 2, 5, 6).


def test_one_team_confirming_keeps_the_match_pending(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    confirm_match(pending, actor=players[0].account)
    confirm_match(pending, actor=players[1].account)

    assert _status(pending) == MatchStatus.PENDING
    assert pending.confirmations.count() == 2
    assert _audits(pending) == []
    assert signals.confirmed == []


def test_both_teams_confirming_confirms_the_match(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    confirm_match(pending, actor=players[1].account)

    result = confirm_match(pending, actor=players[3].account)

    assert result.status == MatchStatus.CONFIRMED
    assert _status(pending) == MatchStatus.CONFIRMED
    [audit] = _audits(pending)
    assert audit.action == AuditAction.MATCH_CONFIRM
    assert audit.actor_user == players[3].account
    assert audit.before == {"status": MatchStatus.PENDING}
    assert audit.after == {"status": MatchStatus.CONFIRMED}
    assert signals.confirmed == [pending.pk]


def test_a_repeated_confirm_does_nothing(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    confirm_match(confirmed, actor=players[0].account)
    confirm_match(confirmed, actor=players[1].account)

    assert len(_audits(confirmed)) == 1
    assert signals.confirmed == []
    assert (
        MatchConfirmation.objects.filter(
            match=confirmed, player=players[0], decision=ConfirmationDecision.CONFIRM
        ).count()
        == 1
    )


@pytest.mark.parametrize("role", list(MemberRole))
def test_an_organizer_member_confirms_alone(
    event: Event, role: str, signals: SignalLog
) -> None:
    match: Match = MatchFactory.create(event=event)
    member = _member(event, role)

    confirm_match(match, actor=member)

    assert _status(match) == MatchStatus.CONFIRMED
    [audit] = _audits(match)
    assert audit.actor_user == member
    assert signals.confirmed == [match.pk]


def test_the_owning_application_confirms(event: Event, signals: SignalLog) -> None:
    match: Match = MatchFactory.create(event=event)
    application = approved_application(organizer=event.organizer)

    confirm_match(match, actor=application)

    [audit] = _audits(match)
    assert audit.actor_application == application
    assert audit.actor_user is None
    assert signals.confirmed == [match.pk]


def test_an_organizer_confirm_of_a_confirmed_match_does_nothing(
    event: Event, signals: SignalLog
) -> None:
    match: Match = MatchFactory.create(event=event)
    member = _member(event)
    confirm_match(match, actor=member)

    confirm_match(match, actor=member)

    assert len(_audits(match)) == 1
    assert signals.confirmed == [match.pk]


@pytest.mark.parametrize("status", [MatchStatus.DISPUTED, MatchStatus.VOIDED])
def test_an_organizer_cannot_confirm_a_disputed_or_voided_match(
    event: Event, status: MatchStatus, signals: SignalLog
) -> None:
    match: Match = MatchFactory.create(event=event)
    raw_update(match, status=status)

    with pytest.raises(MatchStateError):
        confirm_match(match, actor=_member(event))

    assert _status(match) == status
    assert _audits(match) == []


@pytest.mark.parametrize("status", [MatchStatus.DISPUTED, MatchStatus.VOIDED])
def test_a_participant_cannot_confirm_a_disputed_or_voided_match(
    pending: Match, players: list[Player], status: MatchStatus
) -> None:
    raw_update(pending, status=status)

    with pytest.raises(MatchStateError):
        confirm_match(pending, actor=players[0].account)

    assert not pending.confirmations.exists()


def test_a_club_admin_cannot_confirm(signals: SignalLog) -> None:
    club = ClubFactory.create()
    match: Match = MatchFactory.create(club=club)
    admin = UserFactory.create()
    ClubRoleFactory.create(club=club, account=admin, role=ClubRoleKind.ADMIN)

    with pytest.raises(MatchActionDenied):
        confirm_match(match, actor=admin)

    assert _status(match) == MatchStatus.PENDING


# Disputes (AC 4).


def test_disputing_a_pending_match(
    pending: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(pending, actor=players[2].account)

    assert _status(pending) == MatchStatus.DISPUTED
    [audit] = _audits(pending)
    assert audit.action == AuditAction.MATCH_DISPUTE
    assert audit.reason == ""
    assert audit.after == {"status": MatchStatus.DISPUTED}
    assert signals.changed == []


def test_disputing_a_confirmed_match_sends_one_change(
    confirmed: Match, players: list[Player], signals: SignalLog
) -> None:
    dispute_match(confirmed, actor=players[1].account)
    dispute_match(confirmed, actor=players[3].account)

    assert _status(confirmed) == MatchStatus.DISPUTED
    assert len(_audits(confirmed, AuditAction.MATCH_DISPUTE)) == 1
    assert signals.changed == [(confirmed.pk, MatchChange.DISPUTE, PLAYED_ON)]
    assert (
        confirmed.confirmations.filter(decision=ConfirmationDecision.DISPUTE).count()
        == 2
    )


def test_a_voided_match_cannot_be_disputed(
    pending: Match, players: list[Player]
) -> None:
    raw_update(pending, status=MatchStatus.VOIDED)

    with pytest.raises(MatchStateError):
        dispute_match(pending, actor=players[0].account)


def test_only_a_participant_disputes(pending: Match, moderator: User) -> None:
    with pytest.raises(MatchActionDenied):
        dispute_match(pending, actor=moderator)


# Dispute outcomes (AC 8). Only the Event's verified Organizer settles one
# (ADR 0034).


@pytest.fixture
def member(event: Event) -> User:
    return _member(event)


@pytest.fixture
def event_confirmed(event: Event, players: list[Player], member: User) -> Match:
    """An Event match the Organizer's member recorded: confirmed at once."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=member,
        event=event,
    ).match


@pytest.fixture
def disputed(event_confirmed: Match, players: list[Player]) -> Match:
    return dispute_match(event_confirmed, actor=players[0].account)


def test_resolving_a_dispute_as_confirmed(
    disputed: Match, member: User, signals: SignalLog
) -> None:
    resolve_dispute(
        disputed, actor=member, outcome=MatchStatus.CONFIRMED, reason=REASON
    )

    assert _status(disputed) == MatchStatus.CONFIRMED
    [audit] = _audits(disputed, AuditAction.DISPUTE_OUTCOME)
    assert audit.actor_user == member
    assert audit.reason == REASON
    assert audit.before == {"status": MatchStatus.DISPUTED}
    assert audit.after == {"status": MatchStatus.CONFIRMED}
    assert signals.confirmed == [disputed.pk]


def test_resolving_a_dispute_as_voided(
    disputed: Match, member: User, signals: SignalLog
) -> None:
    resolve_dispute(disputed, actor=member, outcome="voided", reason=REASON)

    assert _status(disputed) == MatchStatus.VOIDED
    assert len(_audits(disputed, AuditAction.DISPUTE_OUTCOME)) == 1
    assert signals.confirmed == []
    assert signals.changed == []


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_resolution_needs_a_reason(
    disputed: Match, member: User, reason: str
) -> None:
    with pytest.raises(ValueError, match="Give a reason"):
        resolve_dispute(
            disputed, actor=member, outcome=MatchStatus.CONFIRMED, reason=reason
        )

    assert _status(disputed) == MatchStatus.DISPUTED
    assert _audits(disputed, AuditAction.DISPUTE_OUTCOME) == []


@pytest.mark.parametrize("outcome", ["pending", "disputed", "deleted"])
def test_a_resolution_is_confirmed_or_voided(
    disputed: Match, member: User, outcome: str
) -> None:
    with pytest.raises(ValueError, match="confirmed or voided"):
        resolve_dispute(disputed, actor=member, outcome=outcome, reason=REASON)


def test_a_participant_cannot_resolve(disputed: Match, players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            disputed,
            actor=players[0].account,
            outcome=MatchStatus.CONFIRMED,
            reason=REASON,
        )


def test_only_a_disputed_match_is_resolved(
    event_confirmed: Match, member: User
) -> None:
    with pytest.raises(MatchStateError):
        resolve_dispute(
            event_confirmed, actor=member, outcome=MatchStatus.VOIDED, reason=REASON
        )


# Voiding. No service voids a single match on its own (#327); these use the
# test helper to check what ``_void`` does.


def test_voiding_a_pending_match_sends_no_change(
    event: Event, member: User, signals: SignalLog
) -> None:
    match: Match = MatchFactory.create(event=event)

    void_for_test(match, actor=member)

    assert _status(match) == MatchStatus.VOIDED
    assert signals.changed == []


def test_voiding_a_held_match_releases_the_other(
    event_confirmed: Match, member: User, signals: SignalLog
) -> None:
    other: Match = MatchFactory.create(played_on=date(2026, 5, 1))
    raw_update(other, status=MatchStatus.CONFIRMED)
    MatchFlag.objects.create(
        match=other, reason=FlagReason.POSSIBLE_DUPLICATE, duplicate_of=event_confirmed
    )
    assert not other.is_counted()

    void_for_test(event_confirmed, actor=member)

    assert other.is_counted()
    assert signals.changed == [
        (other.pk, MatchChange.RELEASE, date(2026, 5, 1)),
    ]


# Editing (AC 7).


def _edit(match: Match, actor: Any, **overrides: Any) -> Match:
    stored = Match.objects.get(pk=match.pk)
    teams = {
        team: [p.player for p in stored.participants.filter(team=team)] for team in Team
    }
    values: dict[str, Any] = {
        "format": stored.format,
        "team_a": teams[Team.A],
        "team_b": teams[Team.B],
        "games": OTHER_GAMES,
        "played_on": stored.played_on,
    }
    values.update(overrides)
    return edit_match(match, actor=actor, **values)


def test_an_edit_voids_the_old_match_and_creates_a_correction(
    event_confirmed: Match, member: User, signals: SignalLog
) -> None:
    corrected = _edit(event_confirmed, member, played_on=date(2026, 5, 20))

    assert _status(event_confirmed) == MatchStatus.VOIDED
    assert corrected.pk != event_confirmed.pk
    assert corrected.public_code != event_confirmed.public_code
    assert corrected.corrects_id == event_confirmed.pk
    assert corrected.status == MatchStatus.CONFIRMED
    assert corrected.submitted_by_user_id == event_confirmed.submitted_by_user_id
    assert corrected.played_on == date(2026, 5, 20)
    assert [(g.team_a_score, g.team_b_score) for g in corrected.games.all()] == list(
        OTHER_GAMES
    )
    [audit] = _audits(event_confirmed, AuditAction.MATCH_EDIT)
    assert audit.actor_user == member
    assert audit.reason == ""
    assert audit.before == {"status": MatchStatus.CONFIRMED}
    assert audit.after == {
        "status": MatchStatus.VOIDED,
        "corrected_by": corrected.public_code,
    }
    assert _audits(event_confirmed, AuditAction.MATCH_VOID) == []
    assert _audits(corrected) == []
    assert signals.changed == [
        (event_confirmed.pk, MatchChange.EDIT, date(2026, 5, 20))
    ]
    assert signals.confirmed == []


def test_the_change_date_is_the_earlier_one(
    event_confirmed: Match, member: User, signals: SignalLog
) -> None:
    _edit(event_confirmed, member, played_on=date(2026, 6, 2))

    assert signals.changed == [(event_confirmed.pk, MatchChange.EDIT, PLAYED_ON)]


def test_editing_a_pending_match_keeps_it_pending(
    event: Event, member: User, signals: SignalLog
) -> None:
    match: Match = MatchFactory.create(event=event)

    corrected = _edit(match, member)

    assert corrected.status == MatchStatus.PENDING
    assert signals.changed == []
    assert signals.confirmed == []


def test_an_edit_with_invalid_scores_changes_nothing(
    event_confirmed: Match, member: User, signals: SignalLog
) -> None:
    count = Match.objects.count()

    with pytest.raises(MatchValidationError):
        _edit(event_confirmed, member, games=((11, 10),))

    assert _status(event_confirmed) == MatchStatus.CONFIRMED
    assert Match.objects.count() == count
    assert _audits(event_confirmed, AuditAction.MATCH_EDIT) == []
    assert signals.changed == []


def test_an_edit_that_duplicates_another_match_rolls_back(
    event_confirmed: Match,
    event: Event,
    member: User,
    players: list[Player],
    signals: SignalLog,
) -> None:
    other = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=OTHER_GAMES,
        played_on=date(2026, 6, 3),
        submitted_by_user=member,
        event=event,
    ).match
    count = Match.objects.count()
    signals.changed.clear()

    with pytest.raises(DuplicateMatchError):
        _edit(event_confirmed, member, played_on=other.played_on)

    assert _status(event_confirmed) == MatchStatus.CONFIRMED
    assert Match.objects.count() == count
    assert _audits(event_confirmed, AuditAction.MATCH_EDIT) == []
    assert signals.changed == []


def test_an_edit_keeps_the_application_and_external_id(
    event: Event, signals: SignalLog
) -> None:
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    match: Match = MatchFactory.create(
        by_application=True, submitted_by_application=application, event=event
    )
    raw_update(match, status=MatchStatus.CONFIRMED)

    corrected = _edit(match, application, games=((11, 3),))

    assert corrected.submitted_by_application_id == application.pk
    assert corrected.external_id == match.external_id
    [audit] = _audits(match, AuditAction.MATCH_EDIT)
    assert audit.actor_application == application


def test_a_voided_match_cannot_be_edited(event: Event, member: User) -> None:
    match: Match = MatchFactory.create(event=event)
    void_for_test(match, actor=member)

    with pytest.raises(MatchStateError):
        _edit(match, member)


def test_only_the_settling_organizer_edits_a_disputed_match(event: Event) -> None:
    match: Match = MatchFactory.create(event=event)
    raw_update(match, status=MatchStatus.DISPUTED)
    member = _member(event)
    playing = _member(event, role=MemberRole.ADMIN)
    PlayerFactory.create(account=playing)

    # The settling Organizer gives a reason (ADR 0034).
    with pytest.raises(ValueError, match="Give a reason"):
        _edit(match, member, games=((11, 3),))
    # A member whose own Player is in the correction does not settle it.
    with pytest.raises(MatchStateError):
        _edit(
            match,
            playing,
            games=((11, 3),),
            team_a=[Player.objects.get(account=playing)],
        )

    corrected = _edit(match, member, games=((11, 3),), reason=REASON)

    assert corrected.status == MatchStatus.CONFIRMED


def test_a_participant_cannot_edit(pending: Match, players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        _edit(pending, players[0].account)

    assert _status(pending) == MatchStatus.PENDING


# Refusals (AC 12).


def _refusals(pending: Match) -> list[Callable[[], object]]:
    stranger = UserFactory.create()
    return [
        lambda: confirm_match(pending, actor=stranger),
        lambda: dispute_match(pending, actor=stranger),
        lambda: resolve_dispute(
            pending, actor=stranger, outcome=MatchStatus.VOIDED, reason=REASON
        ),
        lambda: _edit(pending, stranger),
    ]


def test_every_refusal_has_the_same_message_and_writes_nothing(
    pending: Match, signals: SignalLog
) -> None:
    for refusal in _refusals(pending):
        with pytest.raises(MatchActionDenied) as error:
            refusal()
        assert str(error.value) == "You are not allowed to change this match."

    assert _status(pending) == MatchStatus.PENDING
    assert not pending.confirmations.exists()
    assert _audits(pending) == []
    assert Match.objects.count() == 1
    assert signals.confirmed == []
    assert signals.changed == []


def test_an_unsaved_match_is_refused(players: list[Player]) -> None:
    with pytest.raises(MatchActionDenied):
        confirm_match(MatchFactory.build(), actor=players[0].account)


# Holds and releases across an edit.


def _held_pair(players: list[Player]) -> tuple[Match, Match, User]:
    """Two confirmed near duplicates, each holding the other, and the
    Organizer member who recorded them."""
    event = EventFactory.create(organizer=verified_organizer())
    member = _member(event)
    values: dict[str, Any] = {
        "format": MatchFormat.DOUBLES,
        "team_a": players[:2],
        "team_b": players[2:],
        "played_on": PLAYED_ON,
        "submitted_by_user": member,
        "event": event,
    }
    first = record_match(games=GAMES, **values).match
    second = record_match(games=OTHER_GAMES, **values).match
    return first, second, member


def test_an_edit_that_still_holds_the_other_sends_it_nothing(
    players: list[Player], signals: SignalLog
) -> None:
    first, second, member = _held_pair(players)
    signals.changed.clear()
    signals.confirmed.clear()

    corrected = _edit(first, member, games=((11, 0), (11, 1)))

    assert not second.is_counted()
    assert not corrected.is_counted()
    assert signals.changed == []
    assert signals.confirmed == [corrected.pk]


def test_an_edit_of_a_held_match_onto_another_day_releases_the_other(
    players: list[Player], signals: SignalLog
) -> None:
    first, second, member = _held_pair(players)
    signals.changed.clear()
    signals.confirmed.clear()

    corrected = _edit(first, member, played_on=date(2026, 6, 4))

    assert second.is_counted()
    assert corrected.is_counted()
    assert signals.changed == [(second.pk, MatchChange.RELEASE, PLAYED_ON)]
    assert signals.confirmed == [corrected.pk]
