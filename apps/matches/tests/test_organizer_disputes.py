"""The Event's verified Organizer settles a dispute on its match (#269).

ADR 0034 makes the Event's verified Organizer the level that settles a
dispute after the players: a member of it, or the approved application
that submitted the match while it still acts for that Organizer. A member
whose Player is in the match's correction chain, or in the correction's
teams, is refused; another member is not. A reason is required and
stored in the Audit Event. Made-up data only.
"""

from datetime import date
from typing import Any

import pytest
from django.contrib.auth.models import Permission

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditAction, AuditEvent
from apps.clubs.models import ClubRoleKind, MemberRole
from apps.clubs.services import unverify_organizer
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches.models import (
    Match,
    MatchConfirmation,
    MatchFormat,
    MatchStatus,
    Team,
)
from apps.matches.permissions import MatchActionDenied, settles_dispute
from apps.matches.services import record_match
from apps.matches.signals import MatchChange
from apps.matches.tests.conftest import SignalLog
from apps.matches.tests.factories import MatchFactory, raw_update
from apps.matches.workflow import (
    MatchStateError,
    ReasonRefused,
    confirm_match,
    dispute_match,
    edit_match,
    resolve_dispute,
)
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7), (11, 9))
REASON = "Made-up reason for a test."


def _member(event: Event, account: User | None = None) -> User:
    member: User = account or UserFactory.create()
    OrganizerMemberFactory.create(
        organizer=event.organizer, account=member, role=MemberRole.MATCH_MANAGER
    )
    return member


def _record(
    players: list[Player],
    event: Event,
    *,
    submitter: User | None = None,
    application: PartnerApplication | None = None,
    club: Any = None,
) -> Match:
    """A confirmed doubles match of ``event``, players 0 and 1 against 2 and 3."""
    return record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=submitter,
        submitted_by_application=application,
        event=event,
        club=club,
    ).match


def _disputed(players: list[Player], event: Event, **kwargs: Any) -> Match:
    if "submitter" not in kwargs and "application" not in kwargs:
        kwargs["submitter"] = _member(event)
    match = _record(players, event, **kwargs)
    return dispute_match(match, actor=players[0].account)


def _audits(match: Match, action: AuditAction) -> list[AuditEvent]:
    return list(
        AuditEvent.objects.filter(target_object_id=str(match.pk), action=action)
    )


def _status(match: Match) -> str:
    return Match.objects.get(pk=match.pk).status


def _unverify(event: Event) -> None:
    verifier: User = UserFactory.create()
    verifier.user_permissions.add(
        Permission.objects.get(
            content_type__app_label="clubs", codename="verify_organizer"
        )
    )
    unverify_organizer(event.organizer, actor=verifier, reason=REASON)


@pytest.fixture
def players() -> list[Player]:
    return [PlayerFactory.create() for _ in range(4)]


@pytest.fixture
def event() -> Event:
    made: Event = EventFactory.create(organizer=verified_organizer())
    return made


# Resolving a dispute (AC 1, 2, 7).


def test_a_member_resolves_a_dispute_as_confirmed(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    match = _disputed(players, event)
    member = _member(event)
    assert not Match.objects.counted().filter(pk=match.pk).exists()
    signals.confirmed.clear()

    resolve_dispute(match, actor=member, outcome=MatchStatus.CONFIRMED, reason=REASON)

    assert _status(match) == MatchStatus.CONFIRMED
    assert Match.objects.counted().filter(pk=match.pk).exists()
    [audit] = _audits(match, AuditAction.DISPUTE_OUTCOME)
    assert audit.actor_user == member
    assert audit.reason == REASON
    assert audit.before == {"status": MatchStatus.DISPUTED}
    assert audit.after == {"status": MatchStatus.CONFIRMED}
    assert signals.confirmed == [match.pk]


def test_a_member_resolves_a_dispute_as_voided(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    match = _disputed(players, event)
    signals.confirmed.clear()

    resolve_dispute(
        match, actor=_member(event), outcome=MatchStatus.VOIDED, reason=REASON
    )

    assert _status(match) == MatchStatus.VOIDED
    [audit] = _audits(match, AuditAction.DISPUTE_OUTCOME)
    assert audit.reason == REASON
    assert signals.confirmed == []


def test_the_owning_application_resolves_its_match(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _disputed(players, event, application=application)

    resolve_dispute(
        match, actor=application, outcome=MatchStatus.CONFIRMED, reason=REASON
    )

    assert _status(match) == MatchStatus.CONFIRMED
    [audit] = _audits(match, AuditAction.DISPUTE_OUTCOME)
    assert audit.actor_application == application


def test_a_granted_application_resolves_its_match(
    players: list[Player], event: Event
) -> None:
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    match = _disputed(players, event, application=application)

    resolve_dispute(match, actor=application, outcome=MatchStatus.VOIDED, reason=REASON)

    assert _status(match) == MatchStatus.VOIDED


# The conflict rule (AC 4).


def test_a_member_who_plays_in_the_match_is_refused(
    players: list[Player], event: Event
) -> None:
    match = _disputed(players, event)
    playing = _member(event, players[3].account)

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=playing, outcome=MatchStatus.CONFIRMED, reason=REASON
        )

    assert _status(match) == MatchStatus.DISPUTED
    assert _audits(match, AuditAction.DISPUTE_OUTCOME) == []
    resolve_dispute(
        match, actor=_member(event), outcome=MatchStatus.CONFIRMED, reason=REASON
    )
    assert _status(match) == MatchStatus.CONFIRMED


def test_a_member_who_played_an_earlier_match_of_the_chain_is_refused(
    players: list[Player], event: Event
) -> None:
    original = _record(players, event, submitter=_member(event))
    # The correction swaps player 1 out, but player 1 played the original.
    newcomer = PlayerFactory.create()
    corrected = edit_match(
        original,
        actor=_member(event),
        format=MatchFormat.DOUBLES,
        team_a=[players[0], newcomer],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
    )
    dispute_match(corrected, actor=players[0].account)
    playing = _member(event, players[1].account)

    assert not settles_dispute(playing, corrected)
    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            corrected, actor=playing, outcome=MatchStatus.CONFIRMED, reason=REASON
        )


# Refusals (AC 5).


def test_a_member_of_another_organizer_is_refused(
    players: list[Player], event: Event
) -> None:
    match = _disputed(players, event)
    other = EventFactory.create(organizer=verified_organizer())

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=_member(other), outcome=MatchStatus.CONFIRMED, reason=REASON
        )

    assert _status(match) == MatchStatus.DISPUTED


def test_a_club_admin_is_refused(players: list[Player], event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match = _disputed(players, event, club=club)
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=club_admin, outcome=MatchStatus.CONFIRMED, reason=REASON
        )


def test_a_member_of_an_unverified_organizer_is_refused(
    players: list[Player], event: Event
) -> None:
    match = _disputed(players, event)
    member = _member(event)
    _unverify(event)

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=member, outcome=MatchStatus.CONFIRMED, reason=REASON
        )

    assert _status(match) == MatchStatus.DISPUTED


def test_an_application_of_an_unverified_organizer_is_refused(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _disputed(players, event, application=application)
    _unverify(event)

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=application, outcome=MatchStatus.CONFIRMED, reason=REASON
        )


def test_an_application_whose_grant_was_removed_is_refused(
    players: list[Player], event: Event
) -> None:
    application = approved_application()
    OrganizerApplicationGrantFactory.create(
        organizer=event.organizer, application=application
    )
    match = _disputed(players, event, application=application)
    OrganizerApplicationGrant.objects.filter(application=application).delete()

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=application, outcome=MatchStatus.CONFIRMED, reason=REASON
        )


def test_another_application_of_the_organizer_is_refused(
    players: list[Player], event: Event
) -> None:
    submitter = approved_application(organizer=event.organizer)
    other = approved_application(organizer=event.organizer)
    match = _disputed(players, event, application=submitter)

    with pytest.raises(MatchActionDenied):
        resolve_dispute(
            match, actor=other, outcome=MatchStatus.CONFIRMED, reason=REASON
        )


def test_nobody_settles_a_self_reported_match(players: list[Player]) -> None:
    match = record_match(
        format=MatchFormat.DOUBLES,
        team_a=players[:2],
        team_b=players[2:],
        games=GAMES,
        played_on=PLAYED_ON,
        submitted_by_user=players[0].account,
    ).match
    confirm_match(match, actor=players[0].account)
    confirm_match(match, actor=players[2].account)
    dispute_match(match, actor=players[0].account)
    organizer_member = _member(EventFactory.create(organizer=verified_organizer()))

    for actor in (organizer_member, players[1].account):
        assert not settles_dispute(actor, match)
        with pytest.raises(MatchActionDenied):
            resolve_dispute(
                match, actor=actor, outcome=MatchStatus.CONFIRMED, reason=REASON
            )


def test_nobody_settles_a_legacy_club_only_match(event: Event) -> None:
    club = ClubFactory.create()
    ClubOrganizerGrantFactory.create(club=club, organizer=event.organizer)
    match: Match = MatchFactory.create(club=club)
    raw_update(match, status=MatchStatus.DISPUTED)
    club_admin: User = UserFactory.create()
    ClubRoleFactory.create(club=club, account=club_admin, role=ClubRoleKind.ADMIN)

    for actor in (_member(event), club_admin):
        with pytest.raises(MatchActionDenied):
            resolve_dispute(
                match, actor=actor, outcome=MatchStatus.CONFIRMED, reason=REASON
            )


# A blank reason (AC 6).


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_blank_reason_is_refused_and_changes_nothing(
    players: list[Player], event: Event, signals: SignalLog, reason: str
) -> None:
    match = _disputed(players, event)
    signals.confirmed.clear()
    confirmations = MatchConfirmation.objects.filter(match=match).count()
    audits = AuditEvent.objects.count()

    with pytest.raises(ValueError, match="Give a reason"):
        resolve_dispute(
            match, actor=_member(event), outcome=MatchStatus.CONFIRMED, reason=reason
        )

    assert _status(match) == MatchStatus.DISPUTED
    assert MatchConfirmation.objects.filter(match=match).count() == confirmations
    assert AuditEvent.objects.count() == audits
    assert signals.confirmed == []


def test_a_member_cannot_resolve_a_match_that_is_not_disputed(
    players: list[Player], event: Event
) -> None:
    match = _record(players, event, submitter=_member(event))

    with pytest.raises(MatchStateError):
        resolve_dispute(
            match, actor=_member(event), outcome=MatchStatus.VOIDED, reason=REASON
        )


# Correcting a disputed match (AC 8, 9).

CORRECTED_GAMES = ((11, 3), (11, 5))


def _correct(match: Match, actor: Any, **overrides: Any) -> Match:
    stored = Match.objects.get(pk=match.pk)
    teams = {
        team: [p.player for p in stored.participants.filter(team=team)] for team in Team
    }
    values: dict[str, Any] = {
        "format": stored.format,
        "team_a": teams[Team.A],
        "team_b": teams[Team.B],
        "games": CORRECTED_GAMES,
        "played_on": stored.played_on,
        "reason": REASON,
    }
    values.update(overrides)
    return edit_match(match, actor=actor, **values)


def test_a_member_corrects_a_disputed_match(
    players: list[Player], event: Event, signals: SignalLog
) -> None:
    match = _disputed(players, event)
    member = _member(event)
    signals.confirmed.clear()

    corrected = _correct(match, member)

    assert _status(match) == MatchStatus.VOIDED
    assert corrected.status == MatchStatus.CONFIRMED
    assert corrected.corrects_id == match.pk
    assert Match.objects.counted().filter(pk=corrected.pk).exists()
    [audit] = _audits(match, AuditAction.MATCH_EDIT)
    assert audit.actor_user == member
    assert audit.reason == REASON
    # The disputed match did not count, so the correction is confirmed news.
    assert signals.confirmed == [corrected.pk]
    assert MatchChange.EDIT not in signals.changes(match)


def test_the_submitting_application_corrects_a_disputed_match(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _disputed(players, event, application=application)

    corrected = _correct(match, application)

    assert corrected.status == MatchStatus.CONFIRMED
    [audit] = _audits(match, AuditAction.MATCH_EDIT)
    assert audit.actor_application == application
    assert audit.reason == REASON


@pytest.mark.parametrize("reason", ["", "   "])
def test_a_correction_needs_a_reason(
    players: list[Player], event: Event, reason: str
) -> None:
    match = _disputed(players, event)
    count = Match.objects.count()

    with pytest.raises(ValueError, match="Give a reason"):
        _correct(match, _member(event), reason=reason)

    assert _status(match) == MatchStatus.DISPUTED
    assert Match.objects.count() == count
    assert _audits(match, AuditAction.MATCH_EDIT) == []


def test_a_member_who_plays_in_the_match_cannot_correct_it(
    players: list[Player], event: Event
) -> None:
    match = _disputed(players, event)
    playing = _member(event, players[2].account)

    with pytest.raises(MatchStateError):
        _correct(match, playing, reason="")
    with pytest.raises(ReasonRefused):
        _correct(match, playing)

    assert _status(match) == MatchStatus.DISPUTED


def test_a_member_who_puts_their_player_in_the_correction_is_refused(
    players: list[Player], event: Event
) -> None:
    match = _disputed(players, event)
    member = _member(event)
    own = PlayerFactory.create(account=member)

    assert not settles_dispute(member, match, new_player_ids=[own.pk])
    with pytest.raises(MatchStateError):
        _correct(match, member, team_a=[players[0], own], reason="")

    assert _status(match) == MatchStatus.DISPUTED


def test_an_application_without_organizer_authority_cannot_correct(
    players: list[Player], event: Event
) -> None:
    application = approved_application(organizer=event.organizer)
    match = _disputed(players, event, application=application)
    _unverify(event)

    with pytest.raises(MatchActionDenied):
        _correct(match, application)

    assert _status(match) == MatchStatus.DISPUTED


def test_a_reason_on_a_match_that_is_not_disputed_is_still_refused(
    players: list[Player], event: Event
) -> None:
    match = _record(players, event, submitter=_member(event))

    with pytest.raises(ReasonRefused):
        _correct(match, _member(event))

    assert _status(match) == MatchStatus.CONFIRMED


def test_a_member_still_corrects_a_confirmed_match_without_a_reason(
    players: list[Player], event: Event
) -> None:
    match = _record(players, event, submitter=_member(event))

    corrected = _correct(match, _member(event), reason="")

    assert corrected.status == MatchStatus.CONFIRMED
    [audit] = _audits(match, AuditAction.MATCH_EDIT)
    assert audit.reason == ""
