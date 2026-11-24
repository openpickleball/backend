"""Who may create a Match, and who may change one (#79).

This is a security-sensitive area (CLAUDE.md, hard rule 8): these rules
decide who may put a match in the ledger and who may change one.

An actor is an Account or a Partner Application. An Account counts only
when it is saved and active. An application counts only when it
is approved. Sandbox status (#74) and scopes (#28) are not checked here.
Anything else (an anonymous user, another type) is refused.

Event authority, for a match with an Event:

- an Account that is a member (any role) of the Event's Organizer, while
  that Organizer is verified;
- an application that the Event's Organizer owns, while that Organizer is
  verified.

Club authority, for a match with a Club:

- an Account with any Club Role at the Club (admin or match manager);
- an application the Club granted (a Club Application Grant).

Self-submission, for a match with no Event and no Club (decision F1): an
Account whose Player plays in the match. An application may never
self-submit.

``can_create_match`` needs event authority for a match with an Event and
club authority for a match with a Club, so a match with both needs both.
A match with neither needs self-submission.

``can_change_match`` covers actors other than moderators. An application
may change only a match it submitted, while it is approved. An Account
may change a match whose Event's verified Organizer it is a member of, or
a match at a Club where it holds the admin Club Role. A match manager, a
self-submitter, and a participant may not. Moderators act through the
change services in ``apps.matches.workflow`` (from #40's admin), not
through this function.

The confirmation workflow (#26, ``apps.matches.workflow``) adds:

- ``participant_player``: the actor's Player when the actor is an active
  Account whose Player plays in the match. A participant may confirm or
  dispute it. The first-party API also uses it to decide who may read one
  match (``get_match``).
- ``can_confirm_as_organizer``: event authority over the stored match's
  Event (as above). Such an actor's confirmation confirms a pending match
  alone. Club authority alone does not, through ``confirm_match``. But
  ``record_match``'s duplicate path does confirm one: any permitted
  submitter of a match with an Event or a Club, a Club's included, whose
  exact duplicate matches a pending match confirms it (user decision).
- ``is_moderator``: an active Account with the ``matches.moderate_match``
  permission. A moderator may void and edit any match and resolve
  disputes.

A refused workflow action raises ``MatchActionDenied``, with fixed text
that never says which rule failed or what state the match is in.

No rule reads payment status or paid entitlements, and no function takes
such input (hard rule 7).

Each rule reads roles, grants, verification, approval, and an Account's
``is_active`` from the database by id, never from an in-memory instance,
so a stale instance cannot pass. An unsaved actor is refused. The reads take no row locks: a role removed while a submission is in flight
can let that one submission through. Locking role rows would serialize
unrelated submissions, so this is accepted.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from oauth2_provider.models import get_application_model

from apps.clubs.models import Club, ClubRole, MemberRole, Organizer, OrganizerMember
from apps.events.models import Event
from apps.matches.models import Match
from apps.players.models import Player

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

    from apps.partners.models import PartnerApplication

type Actor = _User | AnonymousUser | PartnerApplication


@dataclass(frozen=True)
class MatchSubmission:
    """What a permission check needs to know about a new Match.

    ``player_ids`` holds the primary keys of every Player in the match.
    """

    event: Event | None
    club: Club | None
    player_ids: frozenset[int]


class MatchPermissionDenied(PermissionError):
    """The actor may not submit this match.

    The message is fixed text. It never says which rule failed.
    """

    message: ClassVar[str] = "You are not allowed to submit this match."

    def __init__(self) -> None:
        super().__init__(self.message)


def _account(actor: object) -> "_User | None":
    """The actor as a saved, active Account, else None.

    ``is_active`` is read from the database, not from the instance.
    """
    if not isinstance(actor, get_user_model()) or actor.pk is None:
        return None
    # The model, not type(actor): the admin passes request.user, a lazy
    # proxy whose type is not the Account model.
    if not get_user_model().objects.filter(pk=actor.pk, is_active=True).exists():
        return None
    return actor


def _application(actor: object) -> "PartnerApplication | None":
    """The actor as an approved application, else None.

    Approval is read from the database, not from the instance.
    """
    if not isinstance(actor, get_application_model()) or actor.pk is None:
        return None
    # The application model is PartnerApplication (CLAUDE.md ordering rules).
    application = cast("PartnerApplication", actor)
    stored = type(application).objects.filter(
        pk=application.pk, approved_at__isnull=False
    )
    return application if stored.exists() else None


def _verified_organizer_id(event_id: int | None) -> int | None:
    """The Event's Organizer id when that Organizer is verified, else None."""
    if event_id is None:
        return None
    organizer_id: int | None = (
        Event.objects.filter(
            pk=event_id,
            organizer__in=Organizer.objects.filter(verified_at__isnull=False),
        )
        .values_list("organizer_id", flat=True)
        .first()
    )
    return organizer_id


def _account_event_authority(account: "_User", event_id: int | None) -> bool:
    organizer_id = _verified_organizer_id(event_id)
    if organizer_id is None:
        return False
    return OrganizerMember.objects.filter(
        organizer_id=organizer_id, account=account
    ).exists()


def _application_event_authority(
    application: "PartnerApplication", event_id: int | None
) -> bool:
    organizer_id = _verified_organizer_id(event_id)
    if organizer_id is None:
        return False
    return (
        type(application)
        .objects.filter(pk=application.pk, organizer_id=organizer_id)
        .exists()
    )


def _account_club_authority(
    account: "_User", club_id: int | None, roles: tuple[str, ...]
) -> bool:
    if club_id is None:
        return False
    return ClubRole.objects.filter(
        club_id=club_id, account=account, role__in=roles
    ).exists()


def _application_club_authority(
    application: "PartnerApplication", club_id: int | None
) -> bool:
    if club_id is None:
        return False
    return application.club_grants.filter(club_id=club_id).exists()


def can_create_match(actor: Actor, submission: MatchSubmission) -> bool:
    """True when ``actor`` may submit a Match of this shape.

    See the module docstring for the rules.
    """
    event, club = submission.event, submission.club
    account = _account(actor)
    if account is not None:
        if event is None and club is None:
            return Player.objects.filter(
                account=account, pk__in=submission.player_ids
            ).exists()
        if event is not None and not _account_event_authority(account, event.pk):
            return False
        return club is None or _account_club_authority(
            account, club.pk, tuple(MemberRole.values)
        )
    application = _application(actor)
    if application is not None:
        if event is None and club is None:
            return False
        if event is not None and not _application_event_authority(
            application, event.pk
        ):
            return False
        return club is None or _application_club_authority(application, club.pk)
    return False


def check_can_create_match(actor: Actor, submission: MatchSubmission) -> None:
    """Raise ``MatchPermissionDenied`` unless ``actor`` may submit it."""
    if not can_create_match(actor, submission):
        raise MatchPermissionDenied


def can_change_match(actor: Actor, match: Match) -> bool:
    """True when ``actor``, not a moderator, may change ``match``.

    See the module docstring for the rules.
    """
    application = _application(actor)
    if application is not None:
        return (
            match.pk is not None
            and Match.objects.filter(
                pk=match.pk, submitted_by_application_id=application.pk
            ).exists()
        )
    account = _account(actor)
    if account is None or match.pk is None:
        return False
    stored = Match.objects.filter(pk=match.pk).values("event_id", "club_id").first()
    if stored is None:
        return False
    event_id, club_id = stored["event_id"], stored["club_id"]
    if _account_event_authority(account, event_id):
        return True
    return _account_club_authority(account, club_id, (MemberRole.ADMIN,))


MODERATE_PERMISSION = "matches.moderate_match"

# The auth Group a data migration creates for moderators (#40), and its
# exact permissions. The migration copies these literals; it never imports
# them, since a migration must not change when this module does.
MODERATOR_GROUP = "Moderator"
MODERATOR_PERMISSIONS: tuple[str, ...] = (
    "matches.moderate_match",
    "matches.view_match",
    "matches.view_matchflag",
    "clubs.verify_organizer",
    "clubs.view_organizer",
)


class MatchActionDenied(PermissionError):
    """The actor may not confirm, dispute, or change this match.

    The message is fixed text. It never says which rule failed, or
    anything about the match's state.
    """

    message: ClassVar[str] = "You are not allowed to change this match."

    def __init__(self) -> None:
        super().__init__(self.message)


def participant_player(actor: Actor, match: Match) -> Player | None:
    """The actor's Player when the actor is an active Account that plays in
    the saved ``match``, else None."""
    account = _account(actor)
    if account is None or match.pk is None:
        return None
    player: Player | None = Player.objects.filter(
        account=account, match_participations__match_id=match.pk
    ).first()
    return player


def can_confirm_as_organizer(actor: Actor, match: Match) -> bool:
    """True when ``actor`` has event authority over the stored match's Event.

    That is a member (either role) of the Event's Organizer while it is
    verified, or an approved application that Organizer owns. A match with
    no Event has no organizer to confirm it.
    """
    if match.pk is None:
        return False
    event_id: int | None = (
        Match.objects.filter(pk=match.pk).values_list("event_id", flat=True).first()
    )
    if event_id is None:
        return False
    account = _account(actor)
    if account is not None:
        return _account_event_authority(account, event_id)
    application = _application(actor)
    if application is not None:
        return _application_event_authority(application, event_id)
    return False


def is_moderator(actor: Actor) -> bool:
    """True when ``actor`` is an active Account with ``matches.moderate_match``.

    The Account is loaded again, so neither a stale ``is_active`` nor a
    cached permission set on the instance counts.
    """
    account = _account(actor)
    if account is None:
        return False
    fresh = get_user_model().objects.get(pk=account.pk)
    return fresh.has_perm(MODERATE_PERMISSION)
