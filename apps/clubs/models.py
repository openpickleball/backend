"""Clubs, Organizers, and the roles that link Accounts to them.

An Organizer's verified status is security-sensitive (CLAUDE.md, hard rule
8): change it only through ``apps.clubs.services``, which checks the actor
and records an Audit Event. The permission rules that use the roles here
are in ``apps.matches.permissions``.
"""

import unicodedata
from collections.abc import Collection
from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models, transaction
from django.db.models import Q

from apps.audit.access import AuditedAccessModel
from apps.audit.models import AuditAction
from apps.public_ids.codes import CodeType
from apps.public_ids.models import PublicCodeModel

# Messages never include the submitted value (CLAUDE.md, API). Django's
# defaults for these keys do.
_CHOICE_MESSAGES = {"invalid_choice": "Choose one of the listed options."}
_CONTROL_CHARACTER = "Remove control or invalid characters."
_NOT_TEXT = "Enter text."
# Written only by apps.clubs.services.
VERIFICATION_FIELDS = ("verified_at", "verified_by")
_VERIFIER_MEMBER = "The account that verified this organizer cannot be its member."
VERIFIED_DELETE_MESSAGE = "A verified organizer cannot be deleted: unverify it first."
# Control characters (Postgres cannot store NUL) and lone surrogates
# (not encodable as UTF-8).
_REFUSED_CATEGORIES = frozenset({"Cc", "Cs"})


def _clean_text_fields(
    instance: models.Model,
    names: Collection[str],
    skip: Collection[str],
) -> dict[str, list[ValidationError]]:
    """Strip the named text fields and refuse control characters.

    Returns the errors by field name. A value that is not a string is
    refused, since Django's own conversion would turn any value into text.
    """
    errors: dict[str, list[ValidationError]] = {}
    for name in names:
        if name in skip:
            continue
        value = getattr(instance, name)
        if not isinstance(value, str):
            errors[name] = [ValidationError(_NOT_TEXT)]
            continue
        value = value.strip()
        setattr(instance, name, value)
        if any(unicodedata.category(char) in _REFUSED_CATEGORIES for char in value):
            errors[name] = [ValidationError(_CONTROL_CHARACTER)]
    return errors


class _LinkMessagesModel(models.Model):
    """Gives each link's own ``invalid`` message for an id of the wrong type.

    ``ForeignKey.to_python`` converts the id with the target's primary key
    field, whose message repeats the submitted value (CLAUDE.md, API).
    """

    class Meta:
        abstract = True

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        try:
            super().clean_fields(exclude=exclude)
        except ValidationError as error:
            errors: dict[str, list[ValidationError]] = {}
            for name, field_errors in error.error_dict.items():
                field = _foreign_key(self, name)
                errors[name] = [
                    ValidationError(field.error_messages["invalid"], code="invalid")
                    if field is not None and item.code == "invalid"
                    else item
                    for item in field_errors
                ]
            raise ValidationError(errors) from None


def _foreign_key(
    instance: models.Model, name: str
) -> models.ForeignKey[models.Model, models.Model] | None:
    try:
        field = instance._meta.get_field(name)
    except FieldDoesNotExist:
        return None
    return field if isinstance(field, models.ForeignKey) else None


class _CleanedTextModel(PublicCodeModel):
    """Strips and checks ``TEXT_FIELDS`` before Django's field validation."""

    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ()

    class Meta:
        abstract = True

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        skip = set(exclude or ())
        errors = _clean_text_fields(self, self.TEXT_FIELDS, skip)
        try:
            super().clean_fields(exclude=skip | set(errors))
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)


class VerifiedOrganizerDelete(ValueError):
    """A delete was refused because an Organizer is verified."""

    def __init__(self, public_codes: list[str]) -> None:
        super().__init__(VERIFIED_DELETE_MESSAGE)
        self.public_codes = public_codes


class MemberRole(models.TextChoices):
    """A role an Account holds at an Organizer."""

    ADMIN = "admin", "Admin"
    MATCH_MANAGER = "match_manager", "Match manager"


class ClubRoleKind(models.TextChoices):
    """A role an Account holds at a Club. A Club has only admins (ADR 0021)."""

    ADMIN = "admin", "Admin"


class Club(_CleanedTextModel):
    """A place or group where players play (CONTEXT.md, Club)."""

    public_code_type = CodeType.CLUB
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("name", "city", "region")

    name = models.CharField(max_length=200)
    city = models.CharField(max_length=100, blank=True)
    region = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                # Refuses "" and whitespace only.
                condition=~Q(name__regex=r"^\s*$"),
                name="clubs_club_name_not_empty",
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never a name, for one rule with Organizer.
        return self.public_code


class Organizer(_CleanedTextModel):
    """A person or a body that runs events (CONTEXT.md, Organizer).

    A person who organizes alone is an Organizer with one OrganizerMember.
    A verified Organizer's submissions count as confirmed, so change
    ``verified_at`` and ``verified_by`` only through
    ``apps.clubs.services.verify_organizer`` and ``unverify_organizer``. A
    raw write skips the actor checks and the Audit Event.
    """

    public_code_type = CodeType.ORGANIZER
    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ("name",)
    # Not a field. The services set it on the instance they are given at
    # every call: True when that call changed the verified status, so a
    # caller can report exactly what it did.
    verification_changed: bool = False

    name = models.CharField(max_length=200)
    verified_at = models.DateTimeField(null=True, blank=True, editable=False)
    # PROTECT, like the audit log: an Account that has verified is
    # deactivated, not deleted.
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )

    class Meta:
        permissions = (("verify_organizer", "Can verify or unverify an organizer"),)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~Q(name__regex=r"^\s*$"),
                name="clubs_organizer_name_not_empty",
            ),
            models.CheckConstraint(
                condition=Q(verified_at__isnull=True, verified_by__isnull=True)
                | Q(verified_at__isnull=False, verified_by__isnull=False),
                name="organizer_verified_pair",
            ),
        ]

    def __str__(self) -> str:
        # The OPEN Code, never the name: it may be a person's (ADR 0009).
        return self.public_code

    @property
    def is_verified(self) -> bool:
        return self.verified_at is not None

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, without writing verification on a full save.

        A full save of an existing Organizer writes every loaded field
        except ``verified_at`` and ``verified_by``, so an instance loaded
        before a verify or unverify cannot write the old status back. A
        new Organizer must start unverified, so a copy of a verified one is
        refused before any code is issued.

        These still write verification with no actor check and no Audit
        Event, so never use them for it: an explicit ``update_fields``
        that names either field (the services pass one, under a lock),
        ``update_or_create`` with either field in ``defaults`` (it saves
        with those ``update_fields``), and, like any raw write,
        ``QuerySet.update``, ``bulk_update``, and raw SQL. Change
        verification only through ``apps.clubs.services``.
        """
        if self._state.adding:
            if self.verified_at is not None or self.verified_by_id is not None:
                raise ValueError(
                    "A new organizer starts unverified: verify it through "
                    "apps.clubs.services."
                )
        elif kwargs.get("update_fields") is None:
            deferred = self.get_deferred_fields()
            kwargs["update_fields"] = [
                field.name
                for field in self._meta.concrete_fields
                if not field.primary_key
                and field.attname not in deferred
                and field.name not in VERIFICATION_FIELDS
            ]
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        """Delete an unverified Organizer. A verified one is refused.

        The check reads the stored row under a lock, not this instance.

        Deleting would drop the verification with no Audit Event, so
        unverify it first through the services, which records one.
        ``QuerySet.delete()`` skips this check, like any other raw write.
        """
        with transaction.atomic():
            # Lock and read the stored row: an instance loaded before a
            # verify would otherwise delete a verified Organizer.
            stored = (
                Organizer.objects.select_for_update()
                .filter(pk=self.pk)
                .values_list("verified_at", flat=True)
                .first()
            )
            if self.is_verified or stored is not None:
                raise VerifiedOrganizerDelete([self.public_code])
            return super().delete(*args, **kwargs)


class OrganizerMember(AuditedAccessModel):
    """An Account that acts for an Organizer, with its role.

    Change it only through ``apps.audit.access`` (the admin does), which
    records each add, change, and removal. While the Organizer is
    verified, the Account that verified it cannot be its member, so no
    one verifies an Organizer they act for.
    ``clean()`` and ``save()`` refuse it. ``bulk_create`` and
    ``QuerySet.update`` skip the check, like any other raw write.
    """

    organizer = models.ForeignKey(
        Organizer, on_delete=models.CASCADE, related_name="members"
    )
    # PROTECT: the membership may be the only link from an Organizer that
    # is a person to that person, so an Account is not hard-deleted out
    # from under it (ADR 0009, Retention; #116 plans account deletion).
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="organizer_memberships",
    )
    role = models.CharField(
        max_length=16, choices=MemberRole, error_messages=_CHOICE_MESSAGES
    )

    AUDIT_ADD = AuditAction.ORGANIZER_MEMBER_ADD
    AUDIT_CHANGE = AuditAction.ORGANIZER_MEMBER_CHANGE
    AUDIT_REMOVE = AuditAction.ORGANIZER_MEMBER_REMOVE
    AUDIT_RELATED = ("organizer",)
    # save() locks the Organizer first, and so does the Organizer's delete.
    AUDIT_LOCK_FIRST = ("organizer",)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["organizer", "account"],
                name="clubs_organizermember_unique_pair",
            ),
            models.CheckConstraint(
                condition=Q(role__in=MemberRole.values),
                name="clubs_organizermember_role_valid",
            ),
        ]

    def __str__(self) -> str:
        # Never the Account's email (CLAUDE.md, Personal data).
        return f"{self.organizer.public_code} {self.role}"

    def audit_values(self) -> dict[str, Any]:
        # The Account's row id, never its email; the Organizer's OPEN Code,
        # never its name (ADR 0009).
        return {
            "organizer": self.organizer.public_code,
            "account": self.account_id,
            "role": self.role,
        }

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, refusing the Organizer's verifier as a member.

        The Organizer row is locked first, as the services lock it, so a
        verify and this save run in turn and the rule holds either way.
        """
        with transaction.atomic():
            if self._account_verified_organizer(lock=True):
                raise ValidationError({"account": _VERIFIER_MEMBER})
            super().save(*args, **kwargs)

    def clean(self) -> None:
        if self._account_verified_organizer(lock=False):
            raise ValidationError({"account": _VERIFIER_MEMBER})

    def _account_verified_organizer(self, *, lock: bool) -> bool:
        """True when ``account`` is the Organizer's ``verified_by``."""
        if self.organizer_id is None or self.account_id is None:
            # Field validation reports a missing link.
            return False
        organizers = Organizer.objects.filter(pk=self.organizer_id)
        if lock:
            organizers = organizers.select_for_update()
        verified_by = organizers.values_list("verified_by_id", flat=True).first()
        return verified_by is not None and verified_by == self.account_id


class ClubRole(AuditedAccessModel):
    """An Account's role at a Club.

    A Club admin manages the Club's profile and its Club Organizer Grants.
    A Club Role gives no authority to submit or change a match (ADR 0021).
    Change it only through ``apps.audit.access`` (the admin does), which
    records each add, change, and removal.
    """

    club = models.ForeignKey(Club, on_delete=models.CASCADE, related_name="roles")
    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="club_roles",
    )
    role = models.CharField(
        max_length=16, choices=ClubRoleKind, error_messages=_CHOICE_MESSAGES
    )

    AUDIT_ADD = AuditAction.CLUB_ROLE_ADD
    AUDIT_CHANGE = AuditAction.CLUB_ROLE_CHANGE
    AUDIT_REMOVE = AuditAction.CLUB_ROLE_REMOVE
    AUDIT_RELATED = ("club",)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["club", "account"],
                name="clubs_clubrole_unique_pair",
            ),
            models.CheckConstraint(
                condition=Q(role__in=ClubRoleKind.values),
                name="clubs_clubrole_role_valid",
            ),
        ]

    def __str__(self) -> str:
        # Never the Account's email (CLAUDE.md, Personal data).
        return f"{self.club.public_code} {self.role}"

    def audit_values(self) -> dict[str, Any]:
        # The Account's row id, never its email (ADR 0009).
        return {
            "club": self.club.public_code,
            "account": self.account_id,
            "role": self.role,
        }


class ClubOrganizerGrant(AuditedAccessModel):
    """A Club's consent for one Organizer to name it (ADR 0021).

    ``apps.matches.permissions`` and ``Event.clean()`` use it: a match or
    an Event that names a Club needs a grant from that Club to the
    Event's Organizer. The grant gives consent, not trust: the Organizer
    must still be verified. Change it only through ``apps.audit.access``
    (the admin does), which records each add, change, and removal.
    """

    club = models.ForeignKey(
        Club, on_delete=models.CASCADE, related_name="organizer_grants"
    )
    organizer = models.ForeignKey(
        Organizer, on_delete=models.CASCADE, related_name="club_grants"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    AUDIT_ADD = AuditAction.CLUB_ORGANIZER_GRANT_ADD
    AUDIT_CHANGE = AuditAction.CLUB_ORGANIZER_GRANT_CHANGE
    AUDIT_REMOVE = AuditAction.CLUB_ORGANIZER_GRANT_REMOVE
    AUDIT_RELATED = ("club", "organizer")

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["club", "organizer"],
                name="clubs_cluborganizergrant_unique_pair",
            ),
        ]

    def __str__(self) -> str:
        # Codes, never a name (CLAUDE.md, Personal data).
        return f"{self.club.public_code} {self.organizer.public_code}"

    def audit_values(self) -> dict[str, Any]:
        # OPEN Codes, never a name (ADR 0009).
        return {
            "club": self.club.public_code,
            "organizer": self.organizer.public_code,
        }


class OrganizerReportReason(models.TextChoices):
    """Why an Account reports an Organizer to OPEN Moderators (#271).

    No free text: a report holds one of these and nothing the reporter
    wrote.
    """

    WRONG_RESULTS = "wrong_results", "Posts wrong results"
    FAKE_MATCHES = "fake_matches", "Records matches that were not played"
    IGNORES_DISPUTES = "ignores_disputes", "Ignores disputes"
    MISUSES_PLAYER_DATA = "misuses_player_data", "Misuses player data"
    OTHER = "other", "Other"


_REPORT_ORGANIZER_MESSAGES = {"invalid": "Choose an existing organizer."}
_REPORT_ACCOUNT_MESSAGES = {"invalid": "Choose an existing account."}


class OrganizerReport(_LinkMessagesModel):
    """An Account's report of a verified Organizer to OPEN Moderators (#271).

    Create and close it only through ``apps.clubs.reports``, which checks
    the actor and records an Audit Event. A report is open while
    ``closed_at`` is empty; an Account has at most one open report per
    Organizer (a partial unique constraint). A report changes nothing
    else: no Match, flag, rating, or Organizer field.

    The reporter is held here and in the Audit Event's actor only. No API
    response, email, or Organizer member sees it; the admin shows it to
    moderators as the reporter's Player's OPEN Code.
    """

    organizer = models.ForeignKey(
        Organizer,
        on_delete=models.PROTECT,
        related_name="reports",
        error_messages=_REPORT_ORGANIZER_MESSAGES,
    )
    # CASCADE: the row follows the Account's retention (ADR 0009). The
    # Audit Event's actor (PROTECT) keeps the Account row while it exists.
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        error_messages=_REPORT_ACCOUNT_MESSAGES,
    )
    reason = models.CharField(
        max_length=32,
        choices=OrganizerReportReason,
        error_messages=_CHOICE_MESSAGES,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        default_permissions = ("view",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["organizer", "reporter"],
                condition=Q(closed_at__isnull=True),
                name="clubs_one_open_report_per_account",
                violation_error_message=(
                    "This account already has an open report on this organizer."
                ),
            ),
            models.CheckConstraint(
                condition=Q(reason__in=OrganizerReportReason.values),
                name="clubs_organizerreport_reason_valid",
            ),
        ]

    def __str__(self) -> str:
        # The Organizer's OPEN Code only: never a name or an email.
        return f"Report on {self.organizer.public_code}"

    @property
    def is_open(self) -> bool:
        return self.closed_at is None
