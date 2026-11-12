"""Regression tests for review findings on environment-based settings.

Each case runs a fresh Python process with every settings variable set
explicitly, using the helpers from test_settings.
"""

from django.test import SimpleTestCase

from config.tests.test_settings import (
    LOCAL_DEBUG,
    PRODUCTION_LIKE,
    TEST_SECRET_KEY,
    _check,
    _settings,
)

# Made-up password for tests only. It must never reach any output.
SENTINEL_PASSWORD = "S3CRETPW"


class DollarValuesStayLiteralTests(SimpleTestCase):
    """django-environ treats a leading $ as a reference to another variable.

    Settings must read every value literally (finding F1).
    """

    def test_dollar_key_with_debug_on_is_kept_literally(self) -> None:
        key = "$" + TEST_SECRET_KEY
        values = _settings({**LOCAL_DEBUG, "DJANGO_SECRET_KEY": key})
        self.assertEqual(values["SECRET_KEY"], key)

    def test_dollar_key_with_debug_off_is_kept_literally(self) -> None:
        key = "$" + TEST_SECRET_KEY
        values = _settings({**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": key})
        self.assertEqual(values["SECRET_KEY"], key)

    def test_dollar_home_key_is_not_expanded(self) -> None:
        values = _settings({**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": "$HOME"})
        self.assertEqual(values["SECRET_KEY"], "$HOME")

    def test_dollar_redis_url_is_not_expanded(self) -> None:
        values = _settings({**PRODUCTION_LIKE, "REDIS_URL": "$HOME"})
        self.assertEqual(values["REDIS_URL"], "$HOME")

    def test_dollar_database_url_is_not_expanded(self) -> None:
        # Read literally, '$HOME' is not a database URL, so startup fails.
        result = _check({**PRODUCTION_LIKE, "DATABASE_URL": "$HOME"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DATABASE_URL", result.stderr)


class ListEntriesAreStrippedTests(SimpleTestCase):
    """Finding F3."""

    def test_allowed_hosts_entries_are_stripped(self) -> None:
        values = _settings(
            {**PRODUCTION_LIKE, "DJANGO_ALLOWED_HOSTS": "a.example, b.example "}
        )
        self.assertEqual(values["ALLOWED_HOSTS"], ["a.example", "b.example"])

    def test_csrf_trusted_origins_entries_are_stripped(self) -> None:
        values = _settings(
            {
                **PRODUCTION_LIKE,
                "CSRF_TRUSTED_ORIGINS": "https://a.example, https://b.example",
            }
        )
        self.assertEqual(
            values["CSRF_TRUSTED_ORIGINS"], ["https://a.example", "https://b.example"]
        )

    def test_blank_list_entries_are_dropped(self) -> None:
        values = _settings(
            {
                **PRODUCTION_LIKE,
                "DJANGO_ALLOWED_HOSTS": " ",
                "CSRF_TRUSTED_ORIGINS": " , ",
            }
        )
        self.assertEqual(values["ALLOWED_HOSTS"], [])
        self.assertEqual(values["CSRF_TRUSTED_ORIGINS"], [])


class InvalidServiceUrlTests(SimpleTestCase):
    """Finding F4."""

    def _assert_fails_naming(self, values: dict[str, str], name: str) -> None:
        for args in ((), ("--deploy",)):
            result = _check(values, *args)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("ImproperlyConfigured", result.stderr)
            self.assertIn(name, result.stderr)
            self.assertNotIn(SENTINEL_PASSWORD, result.stderr)
            self.assertNotIn(SENTINEL_PASSWORD, result.stdout)

    def test_blank_database_url_fails(self) -> None:
        self._assert_fails_naming(
            {**PRODUCTION_LIKE, "DATABASE_URL": "   "}, "DATABASE_URL"
        )

    def test_host_only_database_url_fails(self) -> None:
        self._assert_fails_naming(
            {**PRODUCTION_LIKE, "DATABASE_URL": "localhost"}, "DATABASE_URL"
        )

    def test_schemeless_database_url_fails_without_echoing_password(self) -> None:
        url = f"//open:{SENTINEL_PASSWORD}@db.example.org:5432/open"
        self._assert_fails_naming(
            {**PRODUCTION_LIKE, "DATABASE_URL": url}, "DATABASE_URL"
        )

    def test_blank_redis_url_fails(self) -> None:
        self._assert_fails_naming({**PRODUCTION_LIKE, "REDIS_URL": "   "}, "REDIS_URL")


class BlankSecretKeyTests(SimpleTestCase):
    """Finding F5."""

    def test_blank_key_with_debug_off_fails(self) -> None:
        result = _check({**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": "   "})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_non_blank_key_is_kept_unchanged(self) -> None:
        key = f"  {TEST_SECRET_KEY} "
        values = _settings({**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": key})
        self.assertEqual(values["SECRET_KEY"], key)
