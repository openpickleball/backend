from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

import apps.accounts
from apps.accounts.models import User

PASSWORD = "correct-horse-battery-staple-42"


class UserModelSettingsTests(TestCase):
    def test_auth_user_model_setting(self) -> None:
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")

    def test_accounts_app_is_installed(self) -> None:
        self.assertIn("apps.accounts", settings.INSTALLED_APPS)

    def test_get_user_model_returns_accounts_user(self) -> None:
        self.assertIs(get_user_model(), User)

    def test_app_documents_that_player_profile_data_is_not_on_user(self) -> None:
        doc = apps.accounts.__doc__.lower()
        self.assertIn("player", doc)
        self.assertIn("profile", doc)


class UserFieldTests(TestCase):
    def test_removed_fields_do_not_exist(self) -> None:
        for name in ("username", "first_name", "last_name"):
            with self.subTest(field=name), self.assertRaises(FieldDoesNotExist):
                User._meta.get_field(name)

    def test_email_is_the_username_field(self) -> None:
        self.assertEqual(User.USERNAME_FIELD, "email")
        self.assertEqual(User.EMAIL_FIELD, "email")
        self.assertEqual(User.REQUIRED_FIELDS, [])

    def test_email_is_required(self) -> None:
        self.assertFalse(User._meta.get_field("email").blank)

    def test_create_user_without_email_raises(self) -> None:
        with self.assertRaisesMessage(ValueError, "An email address is required."):
            User.objects.create_user("", password=PASSWORD)

    def test_create_user_with_none_email_raises(self) -> None:
        with self.assertRaisesMessage(ValueError, "An email address is required."):
            User.objects.create_user(None, password=PASSWORD)

    def test_full_clean_with_blank_email_raises(self) -> None:
        user = User(email="")
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertIn("email", cm.exception.message_dict)


class UserManagerTests(TestCase):
    def test_create_user_stores_email_lowercased(self) -> None:
        user = User.objects.create_user(" Ann@Example.ORG ", password=PASSWORD)
        user.refresh_from_db()
        self.assertEqual(user.email, "ann@example.org")
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.is_active)

    def test_create_superuser_sets_flags(self) -> None:
        user = User.objects.create_superuser("root@example.com", password=PASSWORD)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)

    def test_create_superuser_rejects_false_flags(self) -> None:
        for flag in ("is_staff", "is_superuser"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                User.objects.create_superuser(
                    "root@example.com", password=PASSWORD, **{flag: False}
                )

    def test_create_superuser_without_email_raises(self) -> None:
        with self.assertRaisesMessage(ValueError, "An email address is required."):
            User.objects.create_superuser("", password=PASSWORD)

    def test_get_by_natural_key_is_case_insensitive(self) -> None:
        user = User.objects.create_user("ann@example.org", password=PASSWORD)
        self.assertEqual(User.objects.get_by_natural_key("ANN@Example.org"), user)

    def test_authenticate_is_case_insensitive(self) -> None:
        user = User.objects.create_user("ann@example.org", password=PASSWORD)
        self.assertEqual(
            authenticate(None, username="ANN@Example.ORG", password=PASSWORD), user
        )

    def test_authenticate_with_wrong_password_fails(self) -> None:
        User.objects.create_user("ann@example.org", password=PASSWORD)
        self.assertIsNone(
            authenticate(None, username="ann@example.org", password="wrong-password")
        )


class EmailUniquenessTests(TestCase):
    def test_create_user_twice_with_case_variant_raises_integrity_error(self) -> None:
        User.objects.create_user("ann@example.org", password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("Ann@Example.org", password=PASSWORD)

    def test_save_bypassing_manager_with_case_variant_raises_integrity_error(
        self,
    ) -> None:
        User.objects.create_user("ann@example.org", password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User(email="Ann@Example.org").save()
        self.assertEqual(User.objects.count(), 1)

    def test_bulk_create_with_case_variant_raises_integrity_error(self) -> None:
        User.objects.create_user("ann@example.org", password=PASSWORD)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(email="ANN@example.org")])

    def test_full_clean_rejects_case_variant(self) -> None:
        User.objects.create_user("ann@example.org", password=PASSWORD)
        user = User(email="Ann@Example.org")
        user.set_password(PASSWORD)
        with self.assertRaises(ValidationError) as cm:
            user.full_clean()
        self.assertIn("already exists", str(cm.exception))

    def test_full_clean_normalizes_email(self) -> None:
        user = User(email="Bea@Example.COM")
        user.set_password(PASSWORD)
        user.full_clean()
        self.assertEqual(user.email, "bea@example.com")
