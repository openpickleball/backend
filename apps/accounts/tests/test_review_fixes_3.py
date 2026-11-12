"""Regression tests for review finding V1 on issue #3 (round R5).

NFKC can expand an address past 254 characters after the raw value has
passed the field's max_length check. U+FB00 (LATIN SMALL LIGATURE FF)
becomes "ff".
"""

from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"
LENGTH_MESSAGE = "Email addresses may be at most 254 characters."
FF = "ﬀ"

# 213 raw characters, 413 after NFKC.
EXPANDING = "a@" + ".".join([FF * 25] * 8) + ".com"
# 253 raw characters, exactly 254 after NFKC.
EXPANDS_TO_254 = "a@" + FF + "b" * 61 + "." + "c" * 63 + "." + "d" * 63 + "." + "e" * 60
# Exactly 254 plain ASCII characters.
ASCII_254 = "a@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 63 + "." + "e" * 60


class NormalizedLengthTests(TestCase):
    def test_lengths_of_the_fixtures(self) -> None:
        self.assertEqual(len(EXPANDING), 213)
        self.assertEqual(len(User.objects.normalize_email(EXPANDING)), 413)
        self.assertEqual(len(EXPANDS_TO_254), 253)
        self.assertEqual(len(User.objects.normalize_email(EXPANDS_TO_254)), 254)
        self.assertEqual(len(ASCII_254), 254)

    def test_full_clean_rejects_address_that_expands_past_254(self) -> None:
        user = User(email=EXPANDING)
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertIn(LENGTH_MESSAGE, cm.exception.message_dict["email"])

    def test_create_user_rejects_address_that_expands_past_254(self) -> None:
        with self.assertRaisesMessage(ValueError, LENGTH_MESSAGE):
            User.objects.create_user(EXPANDING, password=PASSWORD)
        self.assertEqual(User.objects.count(), 0)

    def test_createsuperuser_noinput_reports_length_error(self) -> None:
        with self.assertRaisesMessage(CommandError, LENGTH_MESSAGE):
            call_command(
                "createsuperuser",
                interactive=False,
                email=EXPANDING,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        self.assertEqual(User.objects.count(), 0)

    def test_normalized_length_of_exactly_254_is_accepted(self) -> None:
        for email in (EXPANDS_TO_254, ASCII_254):
            with self.subTest(raw_length=len(email)):
                user = User(email=email)
                user.set_password(PASSWORD)
                user.full_clean()
                self.assertEqual(len(user.email), 254)
        User.objects.create_user(EXPANDS_TO_254, password=PASSWORD)
        User.objects.create_user(ASCII_254, password=PASSWORD)
        self.assertEqual(User.objects.count(), 2)


class AdminAddFormLengthTests(TestCase):
    superuser: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.superuser = User.objects.create_superuser(
            "admin@example.org", password=PASSWORD
        )

    def setUp(self) -> None:
        self.client.force_login(self.superuser)

    def test_add_form_rejects_address_that_expands_past_254(self) -> None:
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": EXPANDING,
                "usable_password": "true",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, LENGTH_MESSAGE)
        self.assertEqual(User.objects.count(), 1)
