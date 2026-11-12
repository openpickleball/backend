"""Email addresses are ASCII-only after NFKC normalization (round R3).

Enforced by the field validator, the manager, and a database check
constraint, so lookalike (homoglyph) accounts cannot be created.
"""

from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.http.response import HttpResponseBase
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"
ASCII_MESSAGE = "Email addresses may contain only ASCII characters."
DOTLESS = "ınfo@example.org"  # U+0131 LATIN SMALL LETTER DOTLESS I
LONG_S = "inſo@example.org"  # U+017F LATIN SMALL LETTER LONG S, NFKC gives "s"
COMMAND = "django.contrib.auth.management.commands.createsuperuser"


class FakeTTY(StringIO):
    def isatty(self) -> bool:
        return True


class FieldValidatorTests(TestCase):
    def test_full_clean_rejects_non_ascii_email(self) -> None:
        user = User(email=DOTLESS)
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertIn(ASCII_MESSAGE, cm.exception.message_dict["email"])

    def test_full_clean_accepts_long_s_and_folds_it(self) -> None:
        user = User(email=LONG_S)
        user.set_password(PASSWORD)
        user.full_clean()
        self.assertEqual(user.email, "inso@example.org")


class ManagerTests(TestCase):
    def test_create_user_rejects_non_ascii_email(self) -> None:
        with self.assertRaisesMessage(ValueError, ASCII_MESSAGE):
            User.objects.create_user(DOTLESS, password=PASSWORD)
        self.assertEqual(User.objects.count(), 0)

    def test_create_superuser_rejects_non_ascii_email(self) -> None:
        with self.assertRaisesMessage(ValueError, ASCII_MESSAGE):
            User.objects.create_superuser(DOTLESS, password=PASSWORD)

    def test_create_user_accepts_long_s_and_folds_it(self) -> None:
        user = User.objects.create_user(LONG_S, password=PASSWORD)
        user.refresh_from_db()
        self.assertEqual(user.email, "inso@example.org")


class CheckConstraintTests(TestCase):
    def test_raw_save_of_non_ascii_email_raises_integrity_error(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            User(email=DOTLESS).save()

    def test_bulk_create_of_non_ascii_email_raises_integrity_error(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email=DOTLESS)])

    def test_update_to_non_ascii_email_raises_integrity_error(self) -> None:
        user = User.objects.create_user("plain@example.org", password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=user.pk).update(email=DOTLESS)

    def test_raw_save_of_ascii_email_is_accepted(self) -> None:
        User(email="Plain.Raw@Example.org").save()
        self.assertEqual(User.objects.count(), 1)


class AdminAddFormTests(TestCase):
    superuser: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.superuser = User.objects.create_superuser(
            "admin@example.org", password=PASSWORD
        )

    def setUp(self) -> None:
        self.client.force_login(self.superuser)

    def post_add(self, email: str) -> HttpResponseBase:
        return self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": email,
                "usable_password": "true",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )

    def test_add_form_rejects_non_ascii_email(self) -> None:
        response = self.post_add(DOTLESS)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ASCII_MESSAGE)
        self.assertEqual(User.objects.count(), 1)

    def test_change_form_rejects_non_ascii_email(self) -> None:
        other = User.objects.create_user("other@example.org", password=PASSWORD)
        url = reverse("admin:accounts_user_change", args=[other.pk])
        response = self.client.post(
            url,
            {
                "email": DOTLESS,
                "is_active": "on",
                "date_joined_0": "2026-01-01",
                "date_joined_1": "00:00:00",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ASCII_MESSAGE)
        other.refresh_from_db()
        self.assertEqual(other.email, "other@example.org")

    def test_add_form_accepts_long_s_and_folds_it(self) -> None:
        response = self.post_add(LONG_S)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(email="inso@example.org").exists())


class CreateSuperuserTests(TestCase):
    def test_interactive_reprompts_with_ascii_message(self) -> None:
        prompts: list[str] = []
        answers = iter([DOTLESS, "valid.admin@example.org"])

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            return next(answers)

        stderr = StringIO()
        with (
            mock.patch(f"{COMMAND}.input", fake_input, create=True),
            mock.patch(f"{COMMAND}.getpass.getpass", lambda *a, **k: PASSWORD),
        ):
            call_command(
                "createsuperuser",
                interactive=True,
                stdin=FakeTTY(),
                stdout=StringIO(),
                stderr=stderr,
            )
        self.assertIn(ASCII_MESSAGE, stderr.getvalue())
        self.assertEqual(prompts, ["Email address: ", "Email address: "])
        self.assertEqual(
            list(User.objects.values_list("email", flat=True)),
            ["valid.admin@example.org"],
        )
