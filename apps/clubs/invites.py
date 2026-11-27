"""Invites by email to act for an Organizer (#229), and later a Club (#230).

The rules here are shared by every invite model, each a subclass of
``InviteModel`` that adds what the invite is to (an Organizer, a Club)
and the role it gives:

- An invite is to one normalized address (``normalized_invite_email``,
  the Account email rules), and lasts until ``expires_at``.
- Its key exists only in the emailed link. The row stores the key's
  SHA-256 (``hash_invite_key``), so a read of the invite table never
  gives a usable link; the plain key also sits in the queued email's
  body (``QueuedEmail``) until it is sent and deleted, as allauth's keys
  do. ``new_invite_key`` makes one.
- ``find_open_invite`` finds an unexpired invite by its key's hash. A
  key that is empty, longer than ``KEY_MAX_LENGTH``, or not ASCII is
  refused before any query.
- ``invite_matches_account`` holds only when the invite's address is the
  Account's login address (``User.email``) and that address is verified.
  A verified secondary address does not count.
- ``sweep_expired`` deletes the expired invites of one model.

Nothing here logs an address or a key.
"""

import hashlib
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Final

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.accounts.addresses import has_verified_login_address
from apps.accounts.validators import (
    EMAIL_MAX_LENGTH,
    normalize_email_address,
    normalized_email_errors,
)

if TYPE_CHECKING:
    from django.contrib.auth.models import _User

# The longest key a lookup accepts; new_invite_key makes 43 characters.
KEY_MAX_LENGTH: Final = 128
# Fixed text, never the submitted value.
INVALID_EMAIL_MESSAGE: Final = "Enter a valid email address."


class InvalidInviteEmail(ValueError):
    """The address is not a valid Account email. Fixed text."""

    def __init__(self) -> None:
        super().__init__(INVALID_EMAIL_MESSAGE)


class InviteModel(models.Model):
    """An invite to one address, found by its key's hash (see module).

    A subclass adds the link to what it invites to and the role, with
    its own constraints.
    """

    # Normalized like an Account's email (normalized_invite_email).
    email = models.CharField(max_length=EMAIL_MAX_LENGTH)
    # The SHA-256 hex digest of the key; the key itself is never stored.
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    # CASCADE: an invite follows its sender's Account (Account deletion
    # deletes the invites it sent before then).
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="+",
        error_messages={"invalid": "Choose an existing account."},
    )

    class Meta:
        abstract = True

    @property
    def is_open(self) -> bool:
        return self.expires_at > timezone.now()


def new_invite_key() -> str:
    """A new random key for an invite link (URL-safe, 43 characters)."""
    return secrets.token_urlsafe(32)


def hash_invite_key(key: str) -> str:
    """The stored form of an invite key: its SHA-256 hex digest."""
    return hashlib.sha256(key.encode()).hexdigest()


def normalized_invite_email(raw: str) -> str:
    """``raw`` normalized as an Account email, or ``InvalidInviteEmail``."""
    normalized = normalize_email_address(raw)
    if not normalized or normalized_email_errors(normalized):
        raise InvalidInviteEmail
    return normalized


def _usable_key(key: object) -> bool:
    return isinstance(key, str) and 0 < len(key) <= KEY_MAX_LENGTH and key.isascii()


def find_open_invite[M: InviteModel](
    queryset: models.QuerySet[M], key: str, *, now: datetime | None = None
) -> M | None:
    """The unexpired invite in ``queryset`` whose key is ``key``, or None.

    Pass a ``select_for_update()`` queryset to lock the row.
    """
    if not _usable_key(key):
        return None
    found: M | None = queryset.filter(
        key_hash=hash_invite_key(key), expires_at__gt=now or timezone.now()
    ).first()
    return found


def invite_matches_account(invite: InviteModel, account: "_User") -> bool:
    """Whether ``account`` may accept ``invite``: the invite's address is
    the Account's login address, and that address is verified."""
    if account.pk is None or invite.email != account.email:
        return False
    return has_verified_login_address(account)


def sweep_expired(model: type[InviteModel], *, now: datetime | None = None) -> int:
    """Delete ``model``'s expired invites and return how many."""
    deleted, _ = model._default_manager.filter(
        expires_at__lte=now or timezone.now()
    ).delete()
    return int(deleted)
