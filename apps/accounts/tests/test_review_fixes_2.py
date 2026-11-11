"""Regression tests for delta-review findings on issue #3 (U1, U2)."""

from django.contrib.auth import aauthenticate, authenticate
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"
# Capital sigma at the end of a word: str.lower() turns it into final sigma
# (U+03C2), while Postgres LOWER() gives plain sigma (U+03C3).
CAPITAL_SIGMA = "a@ΑΣ-b.example.org"
SIGMA = "a@ασ-b.example.org"


class FinalSigmaTests(TestCase):
    """U1: lowercasing matches Postgres LOWER, character by character.

    Rewritten in round R3 (user-approved): email addresses are ASCII-only,
    so Greek addresses are rejected. Normalization still lowercases per
    character, which these tests keep guarding.
    """

    def test_capital_sigma_lowers_per_character_and_is_rejected(self):
        self.assertEqual(User.objects.normalize_email(CAPITAL_SIGMA), SIGMA)
        with self.assertRaisesMessage(
            ValueError, "Email addresses may contain only ASCII characters."
        ):
            User.objects.create_user(CAPITAL_SIGMA, password=PASSWORD)
        self.assertIsNone(authenticate(None, username=SIGMA, password=PASSWORD))

    def test_sigma_variant_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError, "Email addresses may contain only ASCII characters."
        ):
            User.objects.create_user(SIGMA, password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User(email=SIGMA).save()
        self.assertEqual(User.objects.count(), 0)


class NonStringIdentifierTests(TestCase):
    """U2: a non-string login identifier is a failed login, not a crash."""

    @classmethod
    def setUpTestData(cls):
        User.objects.create_user("ann@example.org", password=PASSWORD)

    def test_authenticate_with_int_returns_none(self):
        self.assertIsNone(authenticate(None, username=123, password=PASSWORD))

    def test_authenticate_with_list_returns_none(self):
        self.assertIsNone(
            authenticate(None, username=["ann@example.org"], password=PASSWORD)
        )

    async def test_aauthenticate_with_int_returns_none(self):
        self.assertIsNone(
            await aauthenticate(None, username=123, password=PASSWORD)
        )

    async def test_aauthenticate_with_list_returns_none(self):
        self.assertIsNone(
            await aauthenticate(None, username=["ann@example.org"], password=PASSWORD)
        )
