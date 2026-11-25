"""Who may create a Match, and who may change one (#79).

This is a security-sensitive area (CLAUDE.md, hard rule 8): these rules
decide who may put a match in the ledger and who may change one.

An actor is an Account or a Partner Application. An Account counts only
when it is saved and active. An application counts only when it
is approved. Sandbox status (#74) and scopes (#28) are not checked here.
Anything else (an anonymous user, another type) is refused.

A verified Organizer is the only trusted submitter (ADR 0021, amending
ADR 0012). Event authority over an Event:

- an Account that is a member (either role) of the Event's Organizer,
  while that Organizer is verified;
- an application that the Event's Organizer owns, or that the Organizer
  granted an Organizer Application Grant, while that Organizer is
  verified.

A Club Role gives no authority to submit or change a match.

Club consent: a match that names a Club, or whose Event names one, needs a
Club Organizer Grant from each such Club to the Event's Organizer. The
grant is consent, not trust: the Organizer must still be verified. Both
Clubs are read from the database (the Event's by the Event's id).

Self-submission, for a match with no Event and no Club (decision F1): an
Account whose Player plays in the match. An application may never
self-submit.

``can_create_match`` needs event authority and club consent for a match
with an Event. A match with a Club and no Event is refused for every
actor, since no Organizer stands behind it and it has no Match Type. A
match with neither needs self-submission.

``can_manage_events`` decides who may create, change, and delete an
Organizer's Events through the API (#125): an Account that is a member
(either role) of that Organizer while it is verified, the same Account
rule as event authority. An application may not.

``can_change_match`` covers actors other than moderators. An application
may change only a match it submitted, while it is approved. An Account
may change a match whose Event's verified Organizer it is a member of. A
Club admin, a self-submitter, and a participant may not. An edit also
needs ``can_create_match`` for the corrected match (``edit_match``).
Moderators act through the change services in ``apps.matches.workflow``
(from #40's admin), not through this function.

The confirmation workflow (#26, ``apps.matches.workflow``) adds:

- ``participant_player``: the actor's Player when the actor is an active
  Account whose Player plays in the match. A participant may confirm or
  dispute it. The first-party API also uses it to decide who may read one
  match (``get_match``).
- ``can_confirm_as_organizer``: event authority over the stored match's
  Event (as above, a granted application included). Such an actor's
  confirmation confirms a pending match alone. ``record_match``'s
  duplicate path confirms one too: a permitted submitter of a match with
  an Event, so one with event authority and the Clubs' consent, whose
  exact duplicate matches a pending match confirms it (ADR 0013, ADR
  0021), except in a Submission Batch: a batch submission never confirms
  a pending match and is refused as a duplicate (ADR 0036).
- ``settles_dispute``: the Event's verified Organizer settling a dispute
  (ADR 0034): an actor ``can_change_match`` allows that also passes
  ``can_confirm_as_organizer``, and, for an Account, has no Player in the
  match's correction chain or, for a correction, in the new teams (the
  rule ``moderates`` applies to moderators). Such an actor resolves a
  dispute or corrects a disputed match, with a required reason. A match
  with no Event has no Organizer level.
- ``can_correct_as_player`` and ``corrects_as_player``: a participant
  correcting a self-reported match (no Event, no Club) with the same two
  teams, in either order (#267). The correction is pending until the
  other team confirms it. A player never corrects a match with an Event,
  and never changes who played. The match's status is checked by
  ``edit_match``, not here.
- ``vouch_level``: the level at which an actor vouches for a match held
  by a possible duplicate flag (ADR 0034, #270). An Event match is
  vouched for by its Event's verified Organizer, with the actors and
  conflict rule of ``settles_dispute`` (``VouchLevel.ORGANIZER``). A
  self-reported match is vouched for by its players
  (``participant_player``, ``VouchLevel.PLAYER``). A legacy club-only
  match, and an Event match whose Organizer is not verified, have no
  level: nobody vouches for them.
- ``is_moderator``: an active Account with the ``matches.moderate_match``
  permission. A moderator may void and edit any match, resolve disputes,
  resolve flags, and approve matches, except a match they played in.
- ``moderates``: the check every moderator path in
  ``apps.matches.workflow`` uses. A moderator whose Account has a Player
  in any match of the match's correction chain (the match, the matches it
  corrects, and the matches that correct it) does not moderate it, so no
  one changes their own result as moderator (hard rule 8). The bulk void
  services (``void_submission_batch``, ``void_submitter_matches``) check
  ``is_moderator`` first, then send each match through ``void_match``,
  which applies ``moderates`` to it.

A refused workflow action raises ``MatchActionDenied``, with fixed text
that never says which rule failed or what state the match is in.

No rule reads payment status or paid entitlements, and no function takes
such input (hard rule 7).

Each rule reads roles, grants, verification, approval, and an Account's
``is_active`` from the database by id, never from an in-memory instance,
so a stale instance cannot pass. An unsaved actor, Event, or Club is
refused. The reads take no row locks: a role or grant removed while a
submission is in flight can let that one submission through. Locking role
and grant rows would serialize unrelated submissions, so this is accepted.
"""

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, cast

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from oauth2_provider.models import get_application_model

from apps.clubs.models import Club, ClubOrganizerGrant, Organizer, OrganizerMember
from apps.events.models import Event
from apps.matches.models import Match, MatchParticipant
from apps.partners.models import OrganizerApplicationGrant
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


def _account_acts_for(account: "_User", organizer_id: int) -> bool:
    """True when ``account`` is a member (either role) of ``organizer_id``."""
    return OrganizerMember.objects.filter(
        organizer_id=organizer_id, account=account
    ).exists()


def _account_event_authority(account: "_User", event_id: int | None) -> bool:
    organizer_id = _verified_organizer_id(event_id)
    if organizer_id is None:
        return False
    return _account_acts_for(account, organizer_id)


def _application_event_authority(
    application: "PartnerApplication", event_id: int | None
) -> bool:
    organizer_id = _verified_organizer_id(event_id)
    if organizer_id is None:
        return False
    return _application_acts_for(application, organizer_id)


def _application_acts_for(application: "PartnerApplication", organizer_id: int) -> bool:
    """True when ``organizer_id`` owns ``application`` or granted it an
    Organizer Application Grant (ADR 0021). Read from the database."""
    if (
        type(application)
        .objects.filter(pk=application.pk, organizer_id=organizer_id)
        .exists()
    ):
        return True
    return OrganizerApplicationGrant.objects.filter(
        organizer_id=organizer_id, application_id=application.pk
    ).exists()


def _clubs_consent(organizer_id: int, club_ids: Collection[int | None]) -> bool:
    """True when every Club in ``club_ids`` granted ``organizer_id`` a Club
    Organizer Grant (ADR 0021). Consent is independent of the actor and of
    verification; ``None`` entries name no Club.
    """
    named: set[int] = {club_id for club_id in club_ids if club_id is not None}
    if not named:
        return True
    granted = set(
        ClubOrganizerGrant.objects.filter(
            organizer_id=organizer_id, club_id__in=named
        ).values_list("club_id", flat=True)
    )
    return granted == named


def can_create_match(actor: Actor, submission: MatchSubmission) -> bool:
    """True when ``actor`` may submit a Match of this shape.

    See the module docstring for the rules.
    """
    event, club = submission.event, submission.club
    account = _account(actor)
    application = None if account is not None else _application(actor)
    if account is None and application is None:
        return False
    if event is None:
        if club is not None:
            # A Club and no Event: no Organizer stands behind it (ADR 0021).
            return False
        # A self-submission (decision F1). An application never self-submits.
        return (
            account is not None
            and Player.objects.filter(
                account=account, pk__in=submission.player_ids
            ).exists()
        )
    if event.pk is None or (club is not None and club.pk is None):
        return False
    # One read of the Event decides both checks, so an Event moved to
    # another Organizer mid-check cannot pass authority on one Organizer
    # and consent on another.
    stored = (
        Event.objects.filter(
            pk=event.pk,
            organizer__in=Organizer.objects.filter(verified_at__isnull=False),
        )
        .values("organizer_id", "club_id")
        .first()
    )
    if stored is None:
        return False
    organizer_id: int = stored["organizer_id"]
    authorized = (
        _account_acts_for(account, organizer_id)
        if account is not None
        else application is not None
        and _application_acts_for(application, organizer_id)
    )
    return authorized and _clubs_consent(
        organizer_id, (stored["club_id"], club.pk if club is not None else None)
    )


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
    event_id: int | None = (
        Match.objects.filter(pk=match.pk).values_list("event_id", flat=True).first()
    )
    return _account_event_authority(account, event_id)


def can_manage_events(actor: Actor, organizer_id: int) -> bool:
    """True when ``actor`` may create, change, and delete the Organizer's Events.

    An active Account that is a member (either role) of ``organizer_id``
    while that Organizer is verified (ADR 0021, #125). An application, a
    Club admin, and anyone else may not. Verification, membership, and
    ``is_active`` are read from the database by id.
    """
    account = _account(actor)
    if account is None:
        return False
    if not Organizer.objects.filter(
        pk=organizer_id, verified_at__isnull=False
    ).exists():
        return False
    return _account_acts_for(account, organizer_id)


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
    verified, or an approved application that Organizer owns or granted an
    Organizer Application Grant. A match with no Event has no organizer to
    confirm it. Club consent is not checked: confirming files nothing new
    under a Club's name.
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


def _correction_chain(match_pk: int) -> set[int]:
    """The ids of ``match_pk``'s correction chain, read from the database.

    The match, every match it corrects going back through ``corrects``,
    and every match that corrects it going forward.
    """
    chain = {match_pk}
    frontier = {match_pk}
    while frontier:
        back = Match.objects.filter(
            pk__in=frontier, corrects_id__isnull=False
        ).values_list("corrects_id", flat=True)
        forward = Match.objects.filter(corrects_id__in=frontier).values_list(
            "pk", flat=True
        )
        frontier = {pk for pk in (*back, *forward) if pk is not None} - chain
        chain |= frontier
    return chain


def moderates(
    actor: Actor, match: Match, *, new_player_ids: Collection[int] = ()
) -> bool:
    """True when ``actor`` may act as moderator on the saved ``match``.

    That is a moderator (``is_moderator``) whose Account has no Player in
    any match of ``match``'s correction chain and, for an edit, none among
    ``new_player_ids`` (the corrected teams). Participation is read from
    the database by ids, never from cached relations. An unsaved actor or
    match is refused.
    """
    if match.pk is None or not is_moderator(actor):
        return False
    account = _account(actor)
    if account is None:
        return False
    return not _plays_in(account.pk, match.pk, new_player_ids)


def _plays_in(account_pk: int, match_pk: int, new_player_ids: Collection[int]) -> bool:
    """True when the Account has a Player in any match of ``match_pk``'s
    correction chain or among ``new_player_ids``. Read from the database."""
    if MatchParticipant.objects.filter(
        match_id__in=_correction_chain(match_pk), player__account_id=account_pk
    ).exists():
        return True
    return Player.objects.filter(
        pk__in=list(new_player_ids), account_id=account_pk
    ).exists()


def settles_dispute(
    actor: Actor, match: Match, *, new_player_ids: Collection[int] = ()
) -> bool:
    """True when ``actor`` settles a dispute on the saved ``match`` as its
    Event's verified Organizer (ADR 0034, the level after the players).

    That is an actor ``can_change_match`` allows (a member of the Event's
    verified Organizer, or the approved application that submitted the
    match) that also has event authority over the match's Event
    (``can_confirm_as_organizer``), so an application whose Organizer is no
    longer verified, or whose grant was removed, is refused. A member whose
    Account has a Player in any match of the match's correction chain or,
    for a correction, among ``new_player_ids`` is refused, the rule
    ``moderates`` applies to moderators; another member is not. A match
    with no Event has no Organizer level. The match's status is not
    checked here. Everything is read from the database by ids.
    """
    if match.pk is None:
        return False
    if not (can_change_match(actor, match) and can_confirm_as_organizer(actor, match)):
        return False
    account = _account(actor)
    if account is None:
        # An application: it has no Player.
        return True
    return not _plays_in(account.pk, match.pk, new_player_ids)


def _self_reported(match: Match) -> bool:
    """True when the stored ``match`` has no Event and no Club."""
    return Match.objects.filter(
        pk=match.pk, event_id__isnull=True, club_id__isnull=True
    ).exists()


def can_correct_as_player(actor: Actor, match: Match) -> bool:
    """True when ``actor`` is a participant of the saved, self-reported
    ``match`` (no Event, no Club), so may correct it as a player (#267).

    The teams of the correction are checked by ``corrects_as_player``.
    Read from the database by ids.
    """
    if match.pk is None or participant_player(actor, match) is None:
        return False
    return _self_reported(match)


def _stored_teams(match_pk: int) -> set[frozenset[int]]:
    teams: dict[str, set[int]] = {}
    for player_id, team in MatchParticipant.objects.filter(
        match_id=match_pk
    ).values_list("player_id", "team"):
        teams.setdefault(team, set()).add(player_id)
    return {frozenset(players) for players in teams.values()}


def corrects_as_player(
    actor: Actor,
    match: Match,
    *,
    team_a_ids: Collection[int],
    team_b_ids: Collection[int],
) -> Player | None:
    """The actor's Player when it may correct ``match`` with these teams.

    That is a participant of the saved, self-reported ``match``
    (``can_correct_as_player``) whose correction has the stored two teams,
    in either order (#267). Else None. The match's status is not checked
    here. Everything is read from the database by ids.
    """
    if match.pk is None:
        return None
    player = participant_player(actor, match)
    if player is None or not _self_reported(match):
        return None
    wanted = {frozenset(team_a_ids), frozenset(team_b_ids)}
    if len(wanted) != 2 or wanted != _stored_teams(match.pk):
        return None
    return player


class VouchLevel(StrEnum):
    """The level at which an actor vouches for a held match (ADR 0034)."""

    ORGANIZER = "organizer"
    PLAYER = "player"


def vouch_level(actor: Actor, match: Match) -> VouchLevel | None:
    """The level at which ``actor`` vouches for the saved ``match``, or None.

    ``ORGANIZER`` for a match with an Event when ``settles_dispute``
    allows the actor: a member of the Event's verified Organizer whose
    Player is not in the match's correction chain, or the approved
    application that submitted the match and still acts for that
    Organizer. ``PLAYER`` for a self-reported match (no Event, no Club)
    when the actor is a participant (``participant_player``). None for
    everyone else, and for every actor on a legacy club-only match. The
    match's status and flags are not checked here. Everything is read from
    the database by ids.
    """
    if match.pk is None:
        return None
    if settles_dispute(actor, match):
        return VouchLevel.ORGANIZER
    if participant_player(actor, match) is not None and _self_reported(match):
        return VouchLevel.PLAYER
    return None
