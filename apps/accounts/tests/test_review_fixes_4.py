"""Regression tests for review finding W1 on issue #3 (round R6).

Django's EmailValidator checks the raw value, but clean() stores the
NFKC-normalized value. U+FB00 (LATIN SMALL LIGATURE FF) becomes "ff", so a
62-character label plus the ligature is a valid raw label of 63 characters
that becomes an invalid stored label of 64. The rule is now: normalize,
then validate the normalized value.
"""

from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"
FORMAT_MESSAGE = "Enter a valid email address."
# Raw first domain label: 63 characters (valid). Normalized: 64 (invalid).
EXPANDS_INVALID = "a@" + "ﬀ" + "b" * 62 + ".com"
LONG_S = "inſo@example.org"  # U+017F LATIN SMALL LETTER LONG S


class NormalizedFormatTests(TestCase):
    def test_fixture_label_lengths(self) -> None:
        raw_label = EXPANDS_INVALID.split("@")[1].split(".")[0]
        normalized = User.objects.normalize_email(EXPANDS_INVALID)
        self.assertEqual(len(raw_label), 63)
        self.assertEqual(len(normalized.split("@")[1].split(".")[0]), 64)

    def test_full_clean_rejects_address_whose_normalized_form_is_malformed(
        self,
    ) -> None:
        user = User(email=EXPANDS_INVALID)
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertIn(FORMAT_MESSAGE, cm.exception.message_dict["email"])

    def test_create_user_rejects_address_whose_normalized_form_is_malformed(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            User.objects.create_user(EXPANDS_INVALID, password=PASSWORD)
        self.assertEqual(User.objects.count(), 0)

    def test_createsuperuser_noinput_rejects_it(self) -> None:
        with self.assertRaisesMessage(CommandError, FORMAT_MESSAGE):
            call_command(
                "createsuperuser",
                interactive=False,
                email=EXPANDS_INVALID,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        self.assertEqual(User.objects.count(), 0)

    def test_create_user_rejects_malformed_address(self) -> None:
        with self.assertRaises(ValueError):
            User.objects.create_user("not an email", password=PASSWORD)
        self.assertEqual(User.objects.count(), 0)

    def test_raw_malformed_address_is_reported_once(self) -> None:
        user = User(email="not an email")
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertEqual(cm.exception.message_dict["email"], [FORMAT_MESSAGE])

    def test_long_s_address_is_still_accepted(self) -> None:
        user = User(email=LONG_S)
        user.set_password(PASSWORD)
        user.full_clean()
        self.assertEqual(user.email, "inso@example.org")
        created = User.objects.create_user(LONG_S, password=PASSWORD)
        self.assertEqual(created.email, "inso@example.org")


class AdminAddFormFormatTests(TestCase):
    superuser: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.superuser = User.objects.create_superuser(
            "admin@example.org", password=PASSWORD
        )

    def setUp(self) -> None:
        self.client.force_login(self.superuser)

    def test_add_form_rejects_address_whose_normalized_form_is_malformed(self) -> None:
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": EXPANDS_INVALID,
                "usable_password": "true",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, FORMAT_MESSAGE)
        self.assertEqual(User.objects.count(), 1)
