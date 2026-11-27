"""Account deletion: remove an Account's personal data (#116, ADR 0048).

``delete_account()`` runs in one transaction and removes everything ADR
0009's Retention section does not keep:

- the Account's sessions (each ``UserSession`` row and the Django session
  it names, the app client's token session included), allauth's
  ``EmailAddress`` rows, MFA authenticators, accepted agreement rows,
  OAuth tokens and grants issued to it, its data export (ADR 0051), the
  Organizer Reports it filed (an open one is closed first, with an Audit
  Event), the Organizer Invites addressed to its addresses or sent by it
  (#229), queued emails to its addresses, and the admin log entries tied
  to it;
- its Organizer memberships and Club Roles, through ``apps.audit.access``
  so each removal records an Audit Event. An Organizer whose only member
  is this Account is treated as that person: its name becomes
  ``REMOVED_ORGANIZER_NAME``, with an ``organizer.name_remove`` event,
  and its verification request statements are blanked;
- its Player's personal data. A Player that nothing protects (no Match,
  confirmation, or rating points at it) is deleted with its privacy row.
  Any other Player stays in the Ledger with its OPEN Code: its display
  name becomes ``DELETED_PLAYER_NAME``, its legal name, birth year and
  month, gender division, and city and region are cleared (the encrypted
  ones through their fields), and its privacy settings are reset to
  private, so nothing the player chose is left;
- the Account itself. It is hard-deleted when nothing protects it and it
  owns no Partner Application. Otherwise (it has acted, its Player stays,
  it submitted or vouched for a Match, it verified an Organizer, it owns
  an application, and so on) it stays as a deactivated row with its
  identifying fields cleared: a placeholder email no one can sign up
  with, an unusable password, no staff flags, groups, or permissions, and
  no last login. The placeholder frees the address, so the same person
  can sign up again, which is how a player fixes a birth year and month
  or a gender division entered wrong (ADR 0023).

The Ledger never changes: Matches, their participants, confirmations,
vouches, flags, Submission Batches, and Ratings stay as they are, so a
replay gives every player the same ratings as before. Audit Events stay
(append-only, ADR 0007).

Every Audit Event the deletion records has no actor when the actor is
the deleted Account itself: an event naming a hard-deleted Account fails
the delete at commit (``AuditEvent.actor_user`` uses ``PROTECT``). The
one ``account.delete`` event per deletion targets the Account and says,
in ``after``, whether the Account and the Player were kept. It holds no
identifying value.

Lock order: the Account row (``FOR UPDATE``, as ``create_player`` takes
it, and before ``report_organizer`` can take it ``FOR KEY SHARE``), then
the Player row (``FOR UPDATE``, so ``record_match`` and ``edit_match``,
which take the acting Account and then the Players ``FOR KEY SHARE``,
wait and then see the Account inactive), then every Organizer of its memberships and
of its reports by id (``FOR UPDATE``, as ``unverify_organizer`` and
``apps.audit.access`` take an Organizer before its report or membership
rows), then the membership and report rows.
"""

import secrets
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any

from allauth.account.models import EmailAddress
from allauth.mfa.models import Authenticator
from allauth.usersessions.models import UserSession
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.db import models, router, transaction
from django.db.models import Q
from django.db.models.deletion import Collector, ProtectedError, RestrictedError
from django.db.models.functions import Lower
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_device_grant_model,
    get_grant_model,
    get_id_token_model,
    get_refresh_token_model,
)

from apps.accounts.models import AgreementAcceptance
from apps.audit.access import (
    delete_access_row,
    record_cascaded_removals,
)
from apps.audit.models import AuditAction
from apps.audit.services import record_event
from apps.clubs.models import (
    ClubRole,
    Organizer,
    OrganizerInvite,
    OrganizerMember,
    OrganizerReport,
)
from apps.clubs.reports import delete_reporter_reports
from apps.clubs.verification_requests import blank_statements
from apps.emails.models import QueuedEmail
from apps.exports.services import delete_exports_for
from apps.players.models import Player, PlayerPrivacy, Visibility

if TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, _User

# A Player kept in the Ledger shows this name; others never see it, since
# an inactive Account's Player is shown as a private player (ADR 0015).
DELETED_PLAYER_NAME = "Deleted player"
# The name of an Organizer whose only member deleted their Account.
REMOVED_ORGANIZER_NAME = "Removed organizer"
# A kept Account's email: RFC 2606's reserved .invalid domain, so no mail
# is ever sent to it and nobody can verify it at sign-up.
DELETED_EMAIL_DOMAIN = "deleted.invalid"
DELETED_EMAIL_PREFIX = "deleted-"

# The Player fields Account deletion clears on a kept Player, and the
# value each takes. Display name is required, so it takes a placeholder.
_CLEARED_PLAYER_FIELDS: dict[str, Any] = {
    "display_name": DELETED_PLAYER_NAME,
    "legal_name": "",
    "birth_year": None,
    "birth_month": None,
    "gender_division": "",
    "city": "",
    "region": "",
}
_CLEARED_ACCOUNT_FIELDS = (
    "email",
    "password",
    "is_active",
    "is_staff",
    "is_superuser",
    "last_login",
)


class AccountAlreadyDeleted(ValueError):
    """The stored Account is gone, or inactive with a placeholder email:
    a deletion already ran, so there is nothing to delete."""

    def __init__(self) -> None:
        super().__init__("The account is already deleted.")


@dataclass(frozen=True)
class AccountDeletion:
    """What ``delete_account`` did: whether each row was kept."""

    account_kept: bool
    player_kept: bool | None


def deleted_email() -> str:
    """A fresh placeholder email for a kept Account."""
    return f"{DELETED_EMAIL_PREFIX}{secrets.token_hex(16)}@{DELETED_EMAIL_DOMAIN}"


def delete_account(
    account: "_User", *, actor: "_User | AnonymousUser | None"
) -> AccountDeletion:
    """Delete ``account``'s personal data, as the module docstring says.

    ``actor`` is who asked: the Account itself for self-service deletion.
    Raises ``AccountAlreadyDeleted`` when the stored Account is gone, or
    inactive with a placeholder email, and changes nothing then. An
    Account staff deactivated, with its data still on it, is deleted as
    any other. Call it in a transaction of its own, one Account per
    transaction (the admin's delete wraps it with its ``admin.delete``
    event): its locks last until the outermost transaction ends.
    """
    user_model = get_user_model()
    event_actor = None if actor is None or actor.pk == account.pk else actor
    with transaction.atomic():
        locked = user_model.objects.select_for_update().filter(pk=account.pk).first()
        if locked is None or (not locked.is_active and is_deleted_email(locked.email)):
            raise AccountAlreadyDeleted
        player = Player.objects.select_for_update().filter(account_id=locked.pk).first()
        _lock_organizers(locked)
        log_targets = _log_targets(locked, player)
        addresses = _addresses(locked)

        _end_sessions(locked)
        _remove_memberships(locked, actor=event_actor)
        delete_reporter_reports(locked, actor=event_actor)
        for role in ClubRole.objects.filter(account_id=locked.pk).order_by("pk"):
            delete_access_row(role, actor=event_actor)
        EmailAddress.objects.filter(user_id=locked.pk).delete()
        Authenticator.objects.filter(user_id=locked.pk).delete()
        AgreementAcceptance.objects.filter(account_id=locked.pk).delete()
        _delete_oauth_rows(locked)
        # A build holds the Account FOR KEY SHARE, so it committed before
        # the Account lock above; its email is deleted with the rest.
        delete_exports_for(locked.pk)
        _delete_organizer_invites(locked, addresses)
        _delete_queued_emails(addresses)
        _delete_log_entries(locked, log_targets)

        player_kept: bool | None = None
        if player is not None:
            player_kept = not _deletable(player)
            if player_kept:
                _clear_player(player)
            else:
                player.delete()

        # An application is the Organizer's: keep the Account it points
        # at, cleared, rather than cascade it away (decision 4 on #116).
        owns_application = (
            get_application_model()._default_manager.filter(user_id=locked.pk).exists()
        )
        account_kept = owns_application or not _deletable(locked)
        record_event(
            action=AuditAction.ACCOUNT_DELETE,
            target=locked,
            actor=event_actor,
            after={"account_kept": account_kept, "player_kept": player_kept},
        )
        if account_kept:
            _clear_account(locked)
        else:
            record_cascaded_removals([locked], actor=event_actor)
            locked.delete()
    return AccountDeletion(account_kept=account_kept, player_kept=player_kept)


def is_deleted_email(email: str) -> bool:
    """Whether ``email`` is a placeholder a deletion left on a kept row."""
    return email.startswith(DELETED_EMAIL_PREFIX) and email.endswith(
        f"@{DELETED_EMAIL_DOMAIN}"
    )


def _lock_organizers(account: "_User") -> None:
    """Lock every Organizer of the Account's memberships and reports, by id."""
    ids = set(
        OrganizerMember.objects.filter(account_id=account.pk).values_list(
            "organizer_id", flat=True
        )
    ) | set(
        OrganizerReport.objects.filter(reporter_id=account.pk).values_list(
            "organizer_id", flat=True
        )
    )
    list(
        Organizer.objects.select_for_update()
        .filter(pk__in=ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


def _deletable(obj: models.Model) -> bool:
    """Whether deleting ``obj`` would pass Django's protected links."""
    collector = Collector(using=router.db_for_write(type(obj)), origin=obj)
    try:
        collector.collect([obj])
    except (ProtectedError, RestrictedError):
        return False
    return True


def _addresses(account: "_User") -> set[str]:
    """Every address the Account has, lowercased: its login and allauth's."""
    found = {account.email.lower()}
    found.update(
        email.lower()
        for email in EmailAddress.objects.filter(user_id=account.pk).values_list(
            "email", flat=True
        )
    )
    return found


def _end_sessions(account: "_User") -> None:
    """Delete each of the Account's sessions and its ``UserSession`` row.

    A session made before user sessions were installed has no row. It
    cannot be found without reading every session, so it is left to
    fail: the Account is inactive with an unusable password (or gone), so
    Django refuses it on its next use, and the session sweep deletes it
    when it expires (ADR 0047).
    """
    store = import_module(settings.SESSION_ENGINE).SessionStore
    rows = UserSession.objects.filter(user_id=account.pk)
    for key in rows.values_list("session_key", flat=True):
        store().delete(key)
    rows.delete()


def _remove_memberships(
    account: "_User", *, actor: "_User | AnonymousUser | None"
) -> None:
    """Remove each Organizer membership, naming no one afterwards.

    An Organizer whose only member is this Account is that person (the
    maintainer's decision on #116): its name is replaced first.
    """
    memberships = OrganizerMember.objects.filter(account_id=account.pk).order_by(
        "organizer_id", "pk"
    )
    for membership in memberships:
        organizer = Organizer.objects.select_for_update().get(
            pk=membership.organizer_id
        )
        alone = (
            not OrganizerMember.objects.filter(organizer_id=organizer.pk)
            .exclude(account_id=account.pk)
            .exists()
        )
        if alone:
            # The statement may describe the person, like the name.
            blank_statements(organizer.pk)
        if alone and organizer.name != REMOVED_ORGANIZER_NAME:
            organizer.name = REMOVED_ORGANIZER_NAME
            organizer.save(update_fields=["name"])
            # The field's name only, never its value (ADR 0009).
            record_event(
                action=AuditAction.ORGANIZER_NAME_REMOVE,
                target=organizer,
                actor=actor,
                after={"fields": ["name"]},
            )
        delete_access_row(membership, actor=actor)


def _delete_oauth_rows(account: "_User") -> None:
    """Delete the OAuth tokens and grants issued to the Account, device
    grants included."""
    for model in (
        get_refresh_token_model(),
        get_access_token_model(),
        get_id_token_model(),
        get_grant_model(),
        get_device_grant_model(),
    ):
        model.objects.filter(user_id=account.pk).delete()


def _delete_organizer_invites(account: "_User", addresses: set[str]) -> None:
    """Delete the invites to any of ``addresses`` and those the Account sent.

    An invite's address is stored normalized, as an Account email is.
    """
    OrganizerInvite.objects.filter(
        Q(email__in=sorted(addresses)) | Q(invited_by_id=account.pk)
    ).delete()


def _delete_queued_emails(addresses: set[str]) -> None:
    """Delete queued emails to any of ``addresses`` (they have no link)."""
    QueuedEmail.objects.alias(to_lower=Lower("to")).filter(
        to_lower__in=sorted(addresses)
    ).delete()


def _log_targets(
    account: "_User", player: Player | None
) -> list[tuple[type[models.Model], list[Any]]]:
    """The records that belong to the Account, for its admin log entries."""
    targets: list[tuple[type[models.Model], list[Any]]] = [
        (get_user_model(), [account.pk]),
        (EmailAddress, _pks(EmailAddress.objects.filter(user_id=account.pk))),
        (Authenticator, _pks(Authenticator.objects.filter(user_id=account.pk))),
        (UserSession, _pks(UserSession.objects.filter(user_id=account.pk))),
        (
            AgreementAcceptance,
            _pks(AgreementAcceptance.objects.filter(account_id=account.pk)),
        ),
        (OrganizerMember, _pks(OrganizerMember.objects.filter(account_id=account.pk))),
        (ClubRole, _pks(ClubRole.objects.filter(account_id=account.pk))),
        (
            OrganizerReport,
            _pks(OrganizerReport.objects.filter(reporter_id=account.pk)),
        ),
    ]
    if player is not None:
        targets.append((Player, [player.pk]))
        targets.append(
            (PlayerPrivacy, _pks(PlayerPrivacy.objects.filter(player=player)))
        )
    return targets


def _pks(rows: models.QuerySet[Any]) -> list[Any]:
    return list(rows.values_list("pk", flat=True))


def _delete_log_entries(
    account: "_User", targets: list[tuple[type[models.Model], list[Any]]]
) -> None:
    """Delete the admin log entries tied to the Account (ADR 0009).

    Its own entries, and every entry for a record that belongs to it:
    ``User.__str__`` is the email, so an entry for the Account holds it.
    """
    tied = Q(user_id=account.pk)
    for model, pks in targets:
        if pks:
            tied |= Q(
                content_type=ContentType.objects.get_for_model(model),
                object_id__in=[str(pk) for pk in pks],
            )
    LogEntry.objects.filter(tied).delete()


def _clear_player(player: Player) -> None:
    """Clear a kept Player's personal data and reset its privacy."""
    for name, value in _CLEARED_PLAYER_FIELDS.items():
        setattr(player, name, value)
    player.save(update_fields=list(_CLEARED_PLAYER_FIELDS))
    PlayerPrivacy.objects.filter(player=player).update(
        **dict.fromkeys(PlayerPrivacy.SETTINGS, Visibility.PRIVATE)
    )


def _clear_account(account: "_User") -> None:
    """Clear a kept Account's identifying fields and deactivate it."""
    account.email = deleted_email()
    account.set_unusable_password()
    account.is_active = False
    account.is_staff = False
    account.is_superuser = False
    account.last_login = None
    account.save(update_fields=list(_CLEARED_ACCOUNT_FIELDS))
    account.groups.clear()
    account.user_permissions.clear()
