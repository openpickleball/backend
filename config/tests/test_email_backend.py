"""Tests for the email backend (issue #7).

Nothing is ever sent. With DEBUG on, every message prints to the
console. With DEBUG off, every message is discarded, so no email body
(a reset token, for example) or recipient reaches the logs. Django's test
runner swaps in the locmem backend, which would hide the real setting, so
each case starts a fresh Python process with every settings variable set.
"""

from django.test import SimpleTestCase

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

CONSOLE_BACKEND = 'django.core.mail.backends.console.EmailBackend'
DUMMY_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'

PRINT_EMAIL_BACKEND = """
import django
from django.conf import settings
django.setup()
print(settings.EMAIL_BACKEND)
"""

SEND_MAIL = """
import django
django.setup()
from django.conf import settings
# A closed local port, so a regression to SMTP can never reach a mail server.
settings.EMAIL_HOST = '127.0.0.1'
settings.EMAIL_PORT = 9
from django.core.mail import send_mail
send_mail(
    'Made-up subject',
    'Made-up body',
    'from@example.org',
    ['to@example.org'],
)
"""


def _email_backend(values: dict[str, str]) -> str:
    result = _run(['-c', PRINT_EMAIL_BACKEND], values)
    if result.returncode != 0:
        raise AssertionError(f'settings failed to load:\n{result.stderr}')
    return result.stdout.strip()


class EmailBackendTests(SimpleTestCase):
    def test_local_debug_prints_email_to_the_console(self) -> None:
        self.assertEqual(_email_backend(LOCAL_DEBUG), CONSOLE_BACKEND)

    def test_production_like_discards_email(self) -> None:
        self.assertEqual(_email_backend(PRODUCTION_LIKE), DUMMY_BACKEND)

    def test_send_mail_prints_the_message_and_sends_nothing(self) -> None:
        result = _run(['-c', SEND_MAIL], LOCAL_DEBUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Made-up subject', result.stdout)
        self.assertIn('Made-up body', result.stdout)
