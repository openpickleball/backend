"""``record_match`` checks the submitter's permission first (#79).

A refused submitter gets ``MatchPermissionDenied`` and nothing is written,
before any validation detail, duplicate lookup, lock, or write. So a
refused submitter never learns whether a match exists. Only a verified
Organizer is trusted (ADR 0021): a Club Role gives nothing, a match with
a Club needs an Event and the Club's grant to its Organizer. Made-up data
only.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.models import (
    Club,
    ClubOrganizerGrant,
    ClubRoleKind,
    MemberRole,
    Organizer,
)
from apps.clubs.tests.factories import (
    ClubFactory,
    ClubOrganizerGrantFactory,
    ClubRoleFactory,
    OrganizerFactory,
    OrganizerMemberFactory,
    verified_organizer,
)
from apps.events.models import Event
from apps.events.tests.factories import EventFactory
from apps.matches import services
from apps.matches.models import Game, Match, MatchFlag, MatchFormat, MatchParticipant
from apps.matches.permissions import MatchPermissionDenied
from apps.matches.services import record_match
from apps.partners.models import OrganizerApplicationGrant, PartnerApplication
from apps.partners.tests.factories import (
    OrganizerApplicationGrantFactory,
    PartnerApplicationFactory,
    approved_application,
)
from apps.players.models import Player
from apps.players.tests.factories import PlayerFactory

pytestmark = pytest.mark.django_db

PLAYED_ON = date(2026, 6, 1)
GAMES = ((11, 7),)


@dataclass
class Scene:
    organizer: Organizer
    event: Event
    unverified_event: Event
    club: Club
    ungranted_club: Club
    team_a: list[Player]
    team_b: list[Player]


@pytest.fixture
def scene() -> Scene:
    organizer = verified_organizer()
    club = ClubFactory.create()
    # The Club consents to the Organizer naming it (ADR 0021).
    ClubOrganizerGrantFactory.create(club=club, organizer=organizer)
    return Scene(
        organizer=organizer,
        event=EventFactory.create(organizer=organizer),
        unverified_event=EventFactory.create(organizer=OrganizerFactory.create()),
        club=club,
        ungranted_club=ClubFactory.create(),
        team_a=[PlayerFactory.create()],
        team_b=[PlayerFactory.create()],
    )


def _record(
    scene: Scene, submitter: User | PartnerApplication, **overrides: Any
) -> Match:
    values: dict[str, Any] = {
        "format": MatchFormat.SINGLES,
        "team_a": scene.team_a,
        "team_b": scene.team_b,
        "games": GAMES,
        "played_on": PLAYED_ON,
    }
    if isinstance(submitter, User):
        values["submitted_by_user"] = submitter
    else:
        values["submitted_by_application"] = submitter
    values.update(overrides)
    return record_match(**values).match


def _counts() -> tuple[int, int, int, int]:
    return (
        Match.objects.count(),
        MatchParticipant.objects.count(),
        Game.objects.count(),
        MatchFlag.objects.count(),
    )


def _account() -> User:
    made: User = UserFactory.create()
    return made


def _member(organizer: Organizer, role: str = MemberRole.MATCH_MANAGER) -> User:
    account = _account()
    OrganizerMemberFactory.create(organizer=organizer, account=account, role=role)
    return account


def _club_admin(club: Club) -> User:
    account = _account()
    ClubRoleFactory.create(club=club, account=account, role=ClubRoleKind.ADMIN)
    return account


def _granted(organizer: Organizer, **kwargs: Any) -> PartnerApplication:
    """An approved application ``organizer`` granted (ADR 0021)."""
    application = approved_application(**kwargs)
    OrganizerApplicationGrantFactory.create(
        organizer=organizer, application=application
    )
    return application


Refused = Callable[[Scene], tuple[User | PartnerApplication, dict[str, Any]]]

# Each refused submitter, with the match fields it tries to submit.
REFUSED: dict[str, Refused] = {
    "unverified_organizer_admin": lambda s: (
        _member(s.unverified_event.organizer, MemberRole.ADMIN),
        {"event": s.unverified_event},
    ),
    "unverified_organizer_application": lambda s: (
        approved_application(organizer=s.unverified_event.organizer),
        {"event": s.unverified_event},
    ),
    "unverified_organizer_granted_application": lambda s: (
        _granted(s.unverified_event.organizer),
        {"event": s.unverified_event},
    ),
    # AC 1 and 3: no one submits a match with a Club and no Event.
    "account_without_club_role": lambda s: (_account(), {"club": s.club}),
    "club_admin_club_only": lambda s: (_club_admin(s.club), {"club": s.club}),
    "club_admin_event_and_club": lambda s: (
        _club_admin(s.club),
        {"event": s.event, "club": s.club},
    ),
    "organizer_member_club_only": lambda s: (
        _member(s.organizer, MemberRole.ADMIN),
        {"club": s.club},
    ),
    "organizer_member_with_club_role_club_only": lambda s: (
        _with_club_role(s.club, _member(s.organizer)),
        {"club": s.club},
    ),
    "owned_application_club_only": lambda s: (
        approved_application(organizer=s.organizer),
        {"club": s.club},
    ),
    "granted_application_club_only": lambda s: (
        _granted(s.organizer),
        {"club": s.club},
    ),
    "participant_club_only": lambda s: (s.team_b[0].account, {"club": s.club}),
    # AC 4: the Event's Organizer needs the Club's grant.
    "event_member_ungranted_club": lambda s: (
        _member(s.organizer),
        {"event": s.event, "club": s.ungranted_club},
    ),
    "owned_application_ungranted_club": lambda s: (
        approved_application(organizer=s.organizer),
        {"event": s.event, "club": s.ungranted_club},
    ),
    # AC 2: an application acts only for an Organizer that owns or granted it.
    "application_without_grant": lambda s: (
        approved_application(),
        {"event": s.event},
    ),
    "application_granted_by_another_organizer": lambda s: (
        _granted(verified_organizer()),
        {"event": s.event},
    ),
    "application_self_submission": lambda s: (_granted(s.organizer), {}),
    "unapproved_granted_application": lambda s: (
        _unapproved_granted(s.organizer),
        {"event": s.event},
    ),
    "account_not_playing": lambda s: (_account(), {}),
    "inactive_participant": lambda s: (_inactive(s.team_a[0].account), {}),
}


def _unapproved_granted(organizer: Organizer) -> PartnerApplication:
    application: PartnerApplication = PartnerApplicationFactory.create()
    OrganizerApplicationGrantFactory.create(
        organizer=organizer, application=application
    )
    return application


def _inactive(account: User) -> User:
    account.is_active = False
    account.save(update_fields=["is_active"])
    return account


def _with_club_role(club: Club, account: User) -> User:
    ClubRoleFactory.create(club=club, account=account)
    return account


@pytest.mark.parametrize("kind", sorted(REFUSED))
def test_refused_submitter_writes_nothing(scene: Scene, kind: str) -> None:
    submitter, fields = REFUSED[kind](scene)
    before = _counts()

    with pytest.raises(MatchPermissionDenied) as error:
        _record(scene, submitter, **fields)

    assert str(error.value) == "You are not allowed to submit this match."
    assert _counts() == before


# AC 5: each permitted shape records.

Permitted = Callable[[Scene], tuple[User | PartnerApplication, dict[str, Any]]]

PERMITTED: dict[str, Permitted] = {
    "organizer_admin": lambda s: (
        _member(s.organizer, MemberRole.ADMIN),
        {"event": s.event},
    ),
    "organizer_match_manager": lambda s: (_member(s.organizer), {"event": s.event}),
    "owned_application": lambda s: (
        approved_application(organizer=s.organizer),
        {"event": s.event},
    ),
    "granted_application": lambda s: (_granted(s.organizer), {"event": s.event}),
    "event_member_granted_club": lambda s: (
        _member(s.organizer),
        {"event": s.event, "club": s.club},
    ),
    "event_member_with_club_role": lambda s: (
        _with_club_role(s.club, _member(s.organizer)),
        {"event": s.event, "club": s.club},
    ),
    "owned_application_granted_club": lambda s: (
        approved_application(organizer=s.organizer),
        {"event": s.event, "club": s.club},
    ),
    "granted_application_granted_club": lambda s: (
        _granted(s.organizer),
        {"event": s.event, "club": s.club},
    ),
    "participant": lambda s: (s.team_b[0].account, {}),
}


@pytest.mark.parametrize("kind", sorted(PERMITTED))
def test_permitted_submitter_records(scene: Scene, kind: str) -> None:
    submitter, fields = PERMITTED[kind](scene)

    match = _record(scene, submitter, **fields)

    match.refresh_from_db()
    assert match.event_id == (fields["event"].pk if "event" in fields else None)
    assert match.club_id == (fields["club"].pk if "club" in fields else None)
    assert match.participants.count() == 2


# AC 6: refusal comes before validation, duplicate lookups, locks, and writes.


def test_refusal_beats_an_exact_duplicate(scene: Scene) -> None:
    _record(scene, scene.team_a[0].account)
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(scene, _account())

    assert _counts() == before


def test_refusal_beats_a_repeated_external_id(scene: Scene) -> None:
    application = _granted(scene.organizer)
    _record(scene, application, event=scene.event, external_id="ext-1")
    OrganizerApplicationGrant.objects.filter(application=application).delete()
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(
            scene,
            application,
            event=scene.event,
            external_id="ext-1",
            team_a=[PlayerFactory.create()],
        )

    assert _counts() == before


def test_refusal_beats_invalid_scores(scene: Scene) -> None:
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(scene, _account(), games=((11, 10),))

    assert _counts() == before


def test_refusal_beats_a_model_rule(scene: Scene) -> None:
    with pytest.raises(MatchPermissionDenied):
        _record(scene, _account(), location="a\x00b")


def test_refusal_comes_before_any_lookup_or_lock(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("reached past the permission check")

    for name in ("validate_match", "_advisory_lock", "_exact_match", "_clean_all"):
        monkeypatch.setattr(services, name, fail)

    with pytest.raises(MatchPermissionDenied):
        _record(scene, _account(), club=scene.club)
    with pytest.raises(MatchPermissionDenied):
        _record(scene, _club_admin(scene.club), club=scene.club)


def test_club_only_refusal_beats_invalid_scores(scene: Scene) -> None:
    # AC 3: a Club admin's club-only match with bad scores still gets the
    # fixed refusal, not a validation detail.
    before = _counts()

    with pytest.raises(MatchPermissionDenied) as error:
        _record(scene, _club_admin(scene.club), club=scene.club, games=((11, 10),))

    assert str(error.value) == MatchPermissionDenied.message
    assert _counts() == before


def test_club_grant_is_read_from_the_database(scene: Scene) -> None:
    member = _member(scene.organizer)
    ClubOrganizerGrant.objects.filter(club=scene.club).delete()
    before = _counts()

    with pytest.raises(MatchPermissionDenied):
        _record(scene, member, event=scene.event, club=scene.club)

    assert _counts() == before
