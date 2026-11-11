from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

PASSWORD = "correct-horse-battery-staple-42"
COMMAND = "django.contrib.auth.management.commands.createsuperuser"


class FakeTTY(StringIO):
    def isatty(self):
        return True


class InteractiveCreateSuperuserTests(TestCase):
    def run_interactive(self, answers):
        """Run createsuperuser interactively, answering prompts in order.

        Return the list of prompts shown and the captured stderr.
        """
        prompts = []
        answers = iter(answers)

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(answers)

        def fake_getpass(prompt="Password: ", stream=None):
            prompts.append(prompt)
            return PASSWORD

        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch(f"{COMMAND}.input", fake_input, create=True),
            mock.patch(f"{COMMAND}.getpass.getpass", fake_getpass),
        ):
            call_command(
                "createsuperuser",
                interactive=True,
                stdin=FakeTTY(),
                stdout=stdout,
                stderr=stderr,
            )
        return prompts, stderr.getvalue()

    def test_prompts_for_email_and_never_username(self):
        prompts, _ = self.run_interactive(["Admin@Example.org"])
        self.assertTrue(prompts[0].startswith("Email address"), prompts)
        for prompt in prompts:
            self.assertNotIn("username", prompt.lower())
        user = get_user_model().objects.get()
        self.assertEqual(user.email, "admin@example.org")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.check_password(PASSWORD))

    def test_duplicate_case_variant_is_rejected_then_reprompted(self):
        User = get_user_model()
        User.objects.create_superuser("admin@example.org", password=PASSWORD)
        prompts, stderr = self.run_interactive(
            ["ADMIN@example.org", "second.admin@example.org"]
        )
        self.assertIn("already taken", stderr)
        self.assertEqual(
            [p for p in prompts if p.startswith("Email address")],
            ["Email address: ", "Email address: "],
        )
        self.assertEqual(
            sorted(User.objects.values_list("email", flat=True)),
            ["admin@example.org", "second.admin@example.org"],
        )
