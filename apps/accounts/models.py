from typing import Any, ClassVar

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
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
    matches, external IDs) does not live here; it lives on Player, which
    an Account can claim by linking.
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

    def get_full_name(self) -> str:
        """Accounts carry no name; a person's name lives on Player."""
        return ""

    def get_short_name(self) -> str:
        return self.email
