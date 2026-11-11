"""Regression tests for review findings on issue #3 (T1 to T5, T7)."""

from io import StringIO

from asgiref.sync import sync_to_async
from django.contrib.auth import aauthenticate, authenticate
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"
OTHER_PASSWORD = "other-horse-battery-staple-77"
DOTLESS = "ınfo@example.org"  # U+0131 LATIN SMALL LETTER DOTLESS I
LONG_S = "inſo@example.org"  # U+017F LATIN SMALL LETTER LONG S


class LookupMatchesConstraintTests(TestCase):
    """T1: a lookalike of an existing address can never match or coexist.

    Rewritten in round R3 (user-approved): email addresses are ASCII-only,
    so the dotless-i lookalike can no longer be stored at all. The tests
    still guard the original defect: no lookup can match two rows or crash.
    """

    @classmethod
    def setUpTestData(cls):
        cls.ascii_user = User.objects.create_user("info@example.org", password=PASSWORD)

    def test_get_by_natural_key_returns_the_ascii_user(self):
        with self.assertRaisesMessage(
            ValueError, "Email addresses may contain only ASCII characters."
        ):
            User.objects.create_user(DOTLESS, password=OTHER_PASSWORD)
        self.assertEqual(
            User.objects.get_by_natural_key("info@example.org"), self.ascii_user
        )

    def test_authenticate_ascii_address_returns_its_own_user(self):
        self.assertEqual(
            authenticate(None, username="info@example.org", password=PASSWORD),
            self.ascii_user,
        )

    def test_authenticate_dotless_address_returns_none(self):
        self.assertIsNone(authenticate(None, username=DOTLESS, password=PASSWORD))
        with self.assertRaises(IntegrityError), transaction.atomic():
            User(email=DOTLESS).save()

    def test_createsuperuser_noinput_reports_duplicate_instead_of_crashing(self):
        with self.assertRaisesMessage(CommandError, "already taken"):
            call_command(
                "createsuperuser",
                interactive=False,
                email="INFO@example.org",
                stdout=StringIO(),
                stderr=StringIO(),
            )
        with self.assertRaisesMessage(
            CommandError, "Email addresses may contain only ASCII characters."
        ):
            call_command(
                "createsuperuser",
                interactive=False,
                email=DOTLESS,
                stdout=StringIO(),
                stderr=StringIO(),
            )


class AsyncLookupTests(TestCase):
    """T2: the async lookup is case-insensitive too."""

    async def test_aauthenticate_is_case_insensitive(self):
        user = await sync_to_async(User.objects.create_user)(
            "ann@example.org", password=PASSWORD
        )
        self.assertEqual(
            await aauthenticate(None, username="Ann@Example.org", password=PASSWORD),
            user,
        )


class LookupNormalizesInputTests(TestCase):
    """T3: the lookup strips and lowercases its input like storage does."""

    def test_authenticate_with_surrounding_whitespace(self):
        user = User.objects.create_user(" bob@example.org ", password=PASSWORD)
        self.assertEqual(
            authenticate(None, username=" bob@example.org ", password=PASSWORD), user
        )


class NormalizeEmailUnicodeTests(TestCase):
    """T4: the manager applies NFKC like AbstractBaseUser.clean() does."""

    def test_manager_stores_nfkc_normalized_email(self):
        user = User.objects.create_user(LONG_S, password=PASSWORD)
        user.refresh_from_db()
        self.assertEqual(user.email, "inso@example.org")

    def test_nfkc_equivalent_address_is_a_duplicate(self):
        User.objects.create_user(LONG_S, password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("inso@example.org", password=PASSWORD)


class NameMethodsTests(TestCase):
    """T5: accounts carry no name; Player does."""

    def test_get_full_name_is_empty(self):
        self.assertEqual(User(email="ann@example.org").get_full_name(), "")

    def test_get_short_name_is_the_email(self):
        self.assertEqual(
            User(email="ann@example.org").get_short_name(), "ann@example.org"
        )


class CoverageGapTests(TestCase):
    """T7: coverage for paths the earlier tests did not isolate."""

    def test_full_clean_hits_the_lower_constraint(self):
        # Stored raw, so the exact field-level unique check cannot match the
        # lowercased candidate; only the Lower("email") constraint can.
        User(email="Zed@Example.org").save()
        candidate = User(email="zed@example.org")
        candidate.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            candidate.full_clean()
        self.assertIn(
            "An account with this email address already exists.", str(cm.exception)
        )

    def test_createsuperuser_noinput_rejects_case_variant(self):
        User.objects.create_user("ann@example.org", password=PASSWORD)
        with self.assertRaisesMessage(CommandError, "already taken"):
            call_command(
                "createsuperuser",
                interactive=False,
                email="ANN@Example.org",
                stdout=StringIO(),
                stderr=StringIO(),
            )

    def test_queryset_update_to_case_variant_raises_integrity_error(self):
        User.objects.create_user("ann@example.org", password=PASSWORD)
        other = User.objects.create_user("other@example.org", password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=other.pk).update(email="ANN@Example.org")
