from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

PASSWORD = "correct-horse-battery-staple-42"


class UserAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.superuser = cls.User.objects.create_superuser(
            "admin@example.org", password=PASSWORD
        )

    def setUp(self):
        self.client.force_login(self.superuser)

    def add_form_data(self, email, password=PASSWORD):
        return {
            "email": email,
            "usable_password": "true",
            "password1": password,
            "password2": password,
        }

    def test_user_is_registered(self):
        self.assertTrue(admin.site.is_registered(self.User))

    def test_changelist_renders(self):
        response = self.client.get(reverse("admin:accounts_user_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "admin@example.org")

    def test_add_view_renders_email_and_no_username(self):
        response = self.client.get(reverse("admin:accounts_user_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="email"')
        self.assertNotContains(response, 'name="username"')

    def test_change_view_renders(self):
        response = self.client.get(
            reverse("admin:accounts_user_change", args=[self.superuser.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="email"')
        self.assertNotContains(response, 'name="first_name"')

    def test_add_form_creates_user_by_email(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            self.add_form_data("New.Account@Example.org"),
        )
        self.assertEqual(response.status_code, 302)
        user = self.User.objects.get(email="new.account@example.org")
        self.assertTrue(user.check_password(PASSWORD))

    def test_add_form_rejects_case_variant_duplicate(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            self.add_form_data("ADMIN@Example.org"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(self.User.objects.count(), 1)

    def test_add_form_runs_password_similarity_validator(self):
        response = self.client.post(
            reverse("admin:accounts_user_add"),
            self.add_form_data("similar.person@example.org", "similar.person"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "too similar")
        self.assertEqual(self.User.objects.count(), 1)
