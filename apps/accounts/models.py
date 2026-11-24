from typing import Any, ClassVar

from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models, transaction
from django.db.models import Q, QuerySet, Value
from django.db.models.functions import Lower

from apps.accounts.validators import (
    ASCII_EMAIL_MESSAGE,
    EMAIL_MAX_LENGTH,
    normalize_email_address,
    normalized_email_errors,
    validate_normalized_email,
)


class UserManager(BaseUserManager["User"]):
    """Manager for `User`, keyed on a lowercased email address."""

    use_in_migrations = True

    @classmethod
    def normalize_email(cls, email: str | None) -> str:
        """NFKC-normalize, strip, and lowercase per character (see validators)."""
        return normalize_email_address(email)

    def _create_user(
        self, email: str | None, password: str | None, **extra_fields: Any
    ) -> "User":
        email = self.normalize_email(email)
        if not email:
            raise ValueError("An email address is required.")
        # The same rules as the field validator, on the normalized value.
        # Order: ASCII, length, format; the first broken rule is reported.
        errors = normalized_email_errors(email)
        if errors:
            raise ValueError(errors[0].messages[0])
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self, email: str | None, password: str | None = None, **extra_fields: Any
    ) -> "User":
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(
        self, email: str | None, password: str | None = None, **extra_fields: Any
    ) -> "User":
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra_fields)

    def _natural_key_queryset(self, email: object) -> QuerySet["User"]:
        # Compare with the unique constraint's own expression, Lower("email"),
        # so at most one row can match. email__iexact compares UPPER() on
        # Postgres, which can match two rows the constraint treats as distinct.
        # A non-string identifier matches nothing, so login fails with
        # DoesNotExist (which ModelBackend handles) instead of a TypeError.
        if not isinstance(email, str):
            return self.none()
        return self.alias(email_lower=Lower("email")).filter(
            email_lower=Lower(Value(self.normalize_email(email)))
        )

    def get_by_natural_key(self, email: object) -> "User":
        return self._natural_key_queryset(email).get()

    async def aget_by_natural_key(self, email: object) -> "User":
        return await self._natural_key_queryset(email).aget()


class User(AbstractUser):
    """An OPEN Account: a registered login, identified by email address.

    `User` holds login data only. Player profile data (name, ratings,
    matches, external IDs) does not live here; it lives on Player. Each
    Account creates at most one Player, its own (ADR 0002).
    """

    # Removing the inherited fields: the stubs type them as str.
    username = None  # type: ignore[assignment]
    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]
    email = models.EmailField(
        "email address",
        max_length=EMAIL_MAX_LENGTH,
        unique=True,
        validators=[validate_normalized_email],
    )

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    # An email-keyed manager replaces the stubs' username-based UserManager.
    objects: ClassVar[UserManager] = UserManager()  # type: ignore[assignment]

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                Lower("email"),
                name="accounts_user_email_ci_unique",
                violation_error_message=(
                    "An account with this email address already exists."
                ),
            ),
            # ASCII-only, so writes that skip the manager and the field
            # validator (raw save(), bulk_create, update) cannot store a
            # lookalike address either.
            models.CheckConstraint(
                condition=Q(email__regex=r"^[\x00-\x7F]*$"),
                name="accounts_user_email_ascii",
                violation_error_message=ASCII_EMAIL_MESSAGE,
            ),
        ]

    def __str__(self) -> str:
        return self.email

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Save, dropping allauth's rows for addresses that are not the login.

        When a save changes `email` (staff in the admin, or any code that
        sets it), the Account's allauth EmailAddress rows for any other
        address are deleted in the same transaction. A stale row would keep
        the old address from its real owner: allauth would count it at
        sign-up and refuse to verify it for anyone else. The new address
        gets no row here; the next headless login adds one, unverified, and
        asks for verification. allauth's own change of address saves
        `email` after making the new address its primary, verified row, so
        that row is kept and only the old ones go.

        Raw writes (`QuerySet.update`, `bulk_update`, raw SQL) skip this.
        """
        update_fields = kwargs.get("update_fields")
        checks_email = self.pk is not None and (
            update_fields is None or "email" in update_fields
        )
        if not checks_email:
            super().save(*args, **kwargs)
            return
        with transaction.atomic(using=kwargs.get("using")):
            stored = (
                type(self)
                ._default_manager.filter(pk=self.pk)
                .values_list("email", flat=True)
                .first()
            )
            super().save(*args, **kwargs)
            if stored is not None and stored != self.email:
                email_address = apps.get_model("account", "EmailAddress")
                email_address.objects.filter(user_id=self.pk).alias(
                    email_lower=Lower("email")
                ).exclude(email_lower=Lower(Value(self.email))).delete()

    def get_full_name(self) -> str:
        """Accounts carry no name; a person's name lives on Player."""
        return ""

    def get_short_name(self) -> str:
        return self.email


class Agreement(models.TextChoices):
    """The legal agreements sign-up requires accepting (#222, ADR 0026)."""

    TERMS_OF_SERVICE = "terms_of_service", "Terms of Service"
    PRIVACY_POLICY = "privacy_policy", "Privacy Policy"


class AgreementAcceptance(models.Model):
    """One version of one legal agreement an Account accepted, and when.

    Written at sign-up for the new Account (``record_acceptance``), one row
    per agreement, with the version in settings (ADR 0026). An Account
    accepts each version once. #237 adds rows when a new version is
    accepted later.
    """

    account = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agreement_acceptances",
    )
    agreement = models.CharField(max_length=32, choices=Agreement.choices)
    version = models.PositiveIntegerField()
    accepted_at = models.DateTimeField()

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=["account", "agreement", "version"],
                name="accounts_agreement_acceptance_once",
                violation_error_message="This account already accepted this version.",
            ),
            models.CheckConstraint(
                condition=Q(agreement__in=Agreement.values),
                name="accounts_agreement_acceptance_agreement_valid",
                violation_error_message="Choose one of the listed agreements.",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="accounts_agreement_acceptance_version_from_1",
                violation_error_message="A version starts at 1.",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agreement} version {self.version}"
