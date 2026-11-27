"""The generic invite rules in ``apps.clubs.invites`` (#229, reused by #230).

An invite stores only the SHA-256 of its key, is found by that hash while
unexpired, matches an Account only through its verified login address,
and is swept once expired. Made-up data only.
"""

import hashlib
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.clubs.invites import (
    INVALID_EMAIL_MESSAGE,
    KEY_MAX_LENGTH,
    InvalidInviteEmail,
    find_open_invite,
    hash_invite_key,
    invite_matches_account,
    new_invite_key,
    normalized_invite_email,
    sweep_expired,
)
from apps.clubs.models import MemberRole, OrganizerInvite
from apps.clubs.tests.factories import OrganizerFactory, OrganizerInviteFactory

pytestmark = pytest.mark.django_db


def _verified(account: User, *, verified: bool = True) -> User:
    EmailAddress.objects.create(
        user=account, email=account.email, verified=verified, primary=True
    )
    return account


def test_a_new_key_is_long_random_url_safe_text() -> None:
    keys = {new_invite_key() for _ in range(20)}

    assert len(keys) == 20
    for key in keys:
        assert len(key) >= 40
        assert key.isascii()
        assert all(c.isalnum() or c in "-_" for c in key)


def test_the_stored_form_is_the_sha256_hex_digest() -> None:
    assert hash_invite_key("made-up-key") == (
        hashlib.sha256(b"made-up-key").hexdigest()
    )


def test_an_address_is_normalized_like_an_account_email() -> None:
    assert normalized_invite_email("  Made.Up@Example.COM ") == "made.up@example.com"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not-an-address", "madeıup@example.com", "a" * 250 + "@ex.com"],
)
def test_a_bad_address_is_refused_with_fixed_text(raw: str) -> None:
    with pytest.raises(InvalidInviteEmail) as caught:
        normalized_invite_email(raw)

    assert str(caught.value) == INVALID_EMAIL_MESSAGE
    if raw.strip():
        assert raw.strip() not in str(caught.value)


def test_find_open_invite_finds_by_hash_while_unexpired() -> None:
    key = new_invite_key()
    invite = OrganizerInviteFactory.create(key_hash=hash_invite_key(key))

    assert find_open_invite(OrganizerInvite.objects.all(), key) == invite


def test_find_open_invite_skips_an_expired_invite() -> None:
    key = new_invite_key()
    OrganizerInviteFactory.create(
        key_hash=hash_invite_key(key),
        expires_at=timezone.now() - timedelta(seconds=1),
    )

    assert find_open_invite(OrganizerInvite.objects.all(), key) is None


@pytest.mark.parametrize("key", ["", "x" * (KEY_MAX_LENGTH + 1), "café-key", "\ud800"])
def test_find_open_invite_refuses_a_malformed_key_before_any_lookup(
    key: str, django_assert_num_queries: object
) -> None:
    with django_assert_num_queries(0):  # type: ignore[operator]
        assert find_open_invite(OrganizerInvite.objects.all(), key) is None


def test_an_invite_matches_the_account_with_that_verified_login_address() -> None:
    account = _verified(UserFactory.create())
    invite = OrganizerInviteFactory.create(email=account.email)

    assert invite_matches_account(invite, account) is True


def test_an_invite_does_not_match_an_unverified_login_address() -> None:
    account = _verified(UserFactory.create(), verified=False)
    invite = OrganizerInviteFactory.create(email=account.email)

    assert invite_matches_account(invite, account) is False


def test_an_invite_does_not_match_another_address() -> None:
    account = _verified(UserFactory.create())
    invite = OrganizerInviteFactory.create(email="someone.else@example.com")

    assert invite_matches_account(invite, account) is False


def test_an_invite_does_not_match_a_verified_secondary_address() -> None:
    account = _verified(UserFactory.create(), verified=False)
    EmailAddress.objects.create(
        user=account, email="second.made-up@example.com", verified=True
    )
    invite = OrganizerInviteFactory.create(email="second.made-up@example.com")

    assert invite_matches_account(invite, account) is False


def test_sweep_deletes_only_expired_invites() -> None:
    now = timezone.now()
    expired = OrganizerInviteFactory.create(expires_at=now - timedelta(minutes=1))
    open_invite = OrganizerInviteFactory.create(expires_at=now + timedelta(days=1))

    assert sweep_expired(OrganizerInvite, now=now) == 1
    assert not OrganizerInvite.objects.filter(pk=expired.pk).exists()
    assert OrganizerInvite.objects.filter(pk=open_invite.pk).exists()


def test_one_invite_per_organizer_and_address() -> None:
    organizer = OrganizerFactory.create()
    OrganizerInviteFactory.create(organizer=organizer, email="dup@example.com")

    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizerInviteFactory.create(organizer=organizer, email="dup@example.com")


def test_the_role_must_be_a_member_role() -> None:
    invite = OrganizerInviteFactory.build(role="owner")

    with pytest.raises(ValidationError) as caught:
        invite.full_clean()

    assert caught.value.message_dict["role"] == ["Choose one of the listed options."]


def test_str_names_the_row_only() -> None:
    invite = OrganizerInviteFactory.create(
        email="hidden@example.com", role=MemberRole.ADMIN
    )

    assert str(invite) == f"Organizer invite {invite.pk}"
