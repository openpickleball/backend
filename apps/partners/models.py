"""Partner Applications: the OAuth clients partners use to reach OPEN.

``PartnerApplication`` is django-oauth-toolkit's swapped Application model
(``OAUTH2_PROVIDER_APPLICATION_MODEL``, decision Q3). Approval decides
whether an application can get production tokens (#29), so it is
security-sensitive (CLAUDE.md, hard rule 8): ``approved_at`` and
``approved_by`` are written only through ``update_fields``, by the
approval service #29 adds.
"""

from collections.abc import Collection
from typing import Any, ClassVar, NoReturn

from django.conf import settings
from django.contrib.auth.hashers import identify_hasher
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from oauth2_provider.models import AbstractApplication

from apps.audit.access import AuditedAccessModel
from apps.audit.models import AuditAction

# Written only through update_fields, by the approval service (#29).
APPROVAL_FIELDS = ("approved_at", "approved_by")
_ALWAYS_INSERTED = (
    "A new partner application is always inserted: it cannot be saved "
    "with force_update or update_fields."
)
# Messages never include the submitted value (CLAUDE.md, API).
_BLANK_SECRET = "Enter a client secret."
_HASH_SHAPED_SECRET = (
    "Choose a client secret that does not look like a stored password hash."
)


def _is_hash_shaped(value: object) -> bool:
    """True when ``identify_hasher`` recognizes ``value`` as a hash."""
    if not isinstance(value, str):
        return False
    try:
        identify_hasher(value)
    except ValueError:
        return False
    return True


class PartnerApplication(AbstractApplication):
    """An OAuth client registered by a partner (CONTEXT.md, Partner).

    A new application starts unapproved and in the sandbox. Its client
    secret is always stored hashed: a check constraint refuses
    ``hash_client_secret=False``.
    """

    # PROTECT: an Organizer whose application exists is not deleted out
    # from under it. Null for a platform that is not an Organizer.
    organizer = models.ForeignKey(
        "clubs.Organizer",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="partner_applications",
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    # PROTECT, like Organizer.verified_by: an Account that has approved is
    # deactivated, not deleted.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        editable=False,
    )
    is_sandbox = models.BooleanField(default=True)

    class Meta(AbstractApplication.Meta):
        swappable = "OAUTH2_PROVIDER_APPLICATION_MODEL"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=Q(approved_at__isnull=True, approved_by__isnull=True)
                | Q(approved_at__isnull=False, approved_by__isnull=False),
                name="partners_application_approved_pair",
            ),
            models.CheckConstraint(
                condition=Q(hash_client_secret=True),
                name="partners_application_secret_hashed",
            ),
            models.CheckConstraint(
                condition=~Q(client_secret=""),
                name="partners_application_secret_not_blank",
            ),
        ]

    def __str__(self) -> str:
        # The client_id, never the name: it may name a person.
        return self.client_id

    @property
    def is_approved(self) -> bool:
        return self.approved_at is not None

    @property
    def get_absolute_url(self) -> NoReturn:
        """Absent: a partner application has no page outside the admin.

        DOT's version reverses its application detail view, which OPEN
        does not mount. Raising AttributeError makes ``hasattr`` false, so
        the admin's ``/admin/r/`` shortcut answers 404, not a 500.
        """
        raise AttributeError("A partner application has no page.")

    def _keeps_stored_secret(self) -> bool:
        """True when a save of this row leaves ``client_secret`` out.

        The stored secret is never rewritten. On an existing row a save
        that would write it (a full save, or ``update_fields`` naming it,
        as ``update_or_create`` does) skips it when it is deferred or when
        the value in memory is hash-shaped: the loaded hash, or anything
        that looks like one. A value that is not hash-shaped is a new raw
        secret and is written.
        """
        if self._state.adding:
            return False
        if "client_secret" in self.get_deferred_fields():
            return True
        return _is_hash_shaped(self.client_secret)

    def _raw_secret_error(self) -> str | None:
        """The reason a client secret about to be written is refused.

        A written secret must be raw, since ``ClientSecretField`` hashes
        it: text that is not blank (its hash would let a client in with
        no secret) and that ``identify_hasher`` does not recognize at all.
        A hash-shaped value would be stored as is, and DOT, failing to
        check it, would compare the plaintext. So a copy (``pk = None``,
        ``_state.adding = True``) needs a new raw secret too.
        """
        secret = self.client_secret
        if not isinstance(secret, str) or not secret.strip():
            return _BLANK_SECRET
        if _is_hash_shaped(secret):
            return _HASH_SHAPED_SECRET
        return None

    def clean_fields(self, exclude: Collection[str] | None = None) -> None:
        skip = set(exclude or ())
        errors: dict[str, list[ValidationError]] = {}
        if "client_secret" not in skip and not self._keeps_stored_secret():
            reason = self._raw_secret_error()
            if reason is not None:
                errors["client_secret"] = [ValidationError(reason)]
        try:
            super().clean_fields(exclude=skip | set(errors))
        except ValidationError as error:
            errors = error.update_error_dict(errors)
        if errors:
            raise ValidationError(errors)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, without writing approval on a full save.

        A full save of an existing application writes every loaded field
        except ``approved_at`` and ``approved_by``, so an instance loaded
        before an approval change cannot write the old status back. A new
        application must start unapproved, so a copy of an approved one is
        refused before any write. A new application is always inserted:
        ``force_insert`` is set even when ``asave()`` or a caller passes
        False, and a truthy ``force_update`` or any ``update_fields`` is
        refused before any write, so an instance built with an existing
        pk cannot overwrite that row.

        The stored client secret is never rewritten: a save of an existing
        row leaves it out when it is deferred or hash-shaped (see
        ``_keeps_stored_secret``), whether the save is a full one or names
        it in ``update_fields`` (``update_or_create`` always does). So a
        hash-shaped value given as a rotation is silently not written. Any
        other write of it (a new application, a new non-hash value) must
        be a raw secret, or is refused before any write (see
        ``_raw_secret_error``). ``QuerySet.update``,
        ``bulk_create``, ``bulk_update``, ``loaddata``, and raw SQL skip
        that check; the database still refuses a blank secret.

        An explicit ``update_fields`` naming either approval field writes
        it with no actor check and no Audit Event, as does every raw write
        (``QuerySet.update``, ``bulk_create``, ``bulk_update``,
        ``loaddata``, raw SQL). ``update_or_create`` always names every
        field that overrides ``pre_save``, ``approved_at`` included, but
        writes the value it just loaded under ``select_for_update``, so it
        changes approval only when ``defaults`` sets it. Only the approval
        service (#29) may.
        """
        update_fields = kwargs.get("update_fields")
        if self._state.adding and (
            kwargs.get("force_update") or update_fields is not None
        ):
            # Either would UPDATE whatever row has this pk.
            raise ValueError(_ALWAYS_INSERTED)
        if self._state.adding and (
            self.approved_at is not None or self.approved_by_id is not None
        ):
            raise ValueError(
                "A new partner application starts unapproved: approve it "
                "through the approval service."
            )
        names_secret = update_fields is not None and "client_secret" in update_fields
        writes_secret = update_fields is None or names_secret
        keep_secret = writes_secret and self._keeps_stored_secret()
        if update_fields is not None and names_secret and keep_secret:
            # update_or_create names every field with its own pre_save,
            # client_secret included: the stored hash stays as it is.
            kwargs["update_fields"] = [
                name for name in update_fields if name != "client_secret"
            ]
        if writes_secret and not keep_secret:
            reason = self._raw_secret_error()
            if reason is not None:
                raise ValueError(f"The client secret is refused: {reason}")
        if self._state.adding:
            # A new instance given an existing pk would otherwise UPDATE
            # that row, approval included. It fails on the pk instead.
            # asave() always passes force_insert=False, so a falsy value is
            # replaced; a truthy one (such as a tuple of parents) is kept.
            if not kwargs.get("force_insert"):
                kwargs["force_insert"] = True
        elif update_fields is None:
            deferred = self.get_deferred_fields()
            kwargs["update_fields"] = [
                field.name
                for field in self._meta.concrete_fields
                if not field.primary_key
                and field.attname not in deferred
                and field.name not in APPROVAL_FIELDS
                and not (keep_secret and field.name == "client_secret")
            ]
        super().save(*args, **kwargs)


class OrganizerApplicationGrant(AuditedAccessModel):
    """An Organizer's permission for one Partner Application (ADR 0021).

    ``apps.matches.permissions`` uses it: an approved application with a
    grant may submit and confirm matches for that Organizer's Events
    while the Organizer is verified, as an application the Organizer owns
    may. Change it only through ``apps.audit.access`` (the admin does),
    which records each add, change, and removal.
    """

    organizer = models.ForeignKey(
        "clubs.Organizer",
        on_delete=models.CASCADE,
        related_name="application_grants",
    )
    application = models.ForeignKey(
        PartnerApplication, on_delete=models.CASCADE, related_name="organizer_grants"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    AUDIT_ADD = AuditAction.ORGANIZER_APPLICATION_GRANT_ADD
    AUDIT_CHANGE = AuditAction.ORGANIZER_APPLICATION_GRANT_CHANGE
    AUDIT_REMOVE = AuditAction.ORGANIZER_APPLICATION_GRANT_REMOVE
    AUDIT_RELATED = ("organizer", "application")

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["organizer", "application"],
                name="partners_organizerapplicationgrant_unique_pair",
            ),
        ]

    def __str__(self) -> str:
        # Codes, never a name (CLAUDE.md, Personal data).
        return f"{self.organizer.public_code} {self.application.client_id}"

    def audit_values(self) -> dict[str, Any]:
        # The Organizer's OPEN Code, never its name, and the client id,
        # never the application's name or secret.
        return {
            "organizer": self.organizer.public_code,
            "application": self.application.client_id,
        }
