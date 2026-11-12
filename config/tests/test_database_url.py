"""DATABASE_URL must be a full database URL (review finding G2).

Only a scheme django-environ knows, in //host form, with a database name
is accepted. Anything else fails at startup naming DATABASE_URL and never
echoes the value.
"""

from django.test import SimpleTestCase

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _check, _settings

# Made-up password for tests only. It must never reach any output.
SENTINEL_PASSWORD = "S3CRETPW"

BAD_URLS = (
    "postgres:open@host",
    "postgres://",
    "localhost:5432",
    "db.example.org:5432/open",
    f"open:{SENTINEL_PASSWORD}@db.example.org:5432/open",
    f"postgress://open:{SENTINEL_PASSWORD}@db.example.org:5432/open",
    f"https://open:{SENTINEL_PASSWORD}@db.example.org:5432/open",
    f"postgres://open:{SENTINEL_PASSWORD}@db.example.org:5432/",
)


class MalformedDatabaseUrlTests(SimpleTestCase):
    def test_malformed_urls_fail_naming_the_variable(self) -> None:
        for url in BAD_URLS:
            with self.subTest(url=url.replace(SENTINEL_PASSWORD, "<password>")):
                result = _check({**PRODUCTION_LIKE, "DATABASE_URL": url})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("ImproperlyConfigured", result.stderr)
                self.assertIn("DATABASE_URL", result.stderr)
                self.assertNotIn(SENTINEL_PASSWORD, result.stderr)
                self.assertNotIn(SENTINEL_PASSWORD, result.stdout)


class ValidDatabaseUrlTests(SimpleTestCase):
    def test_env_example_url_loads(self) -> None:
        values = _settings(LOCAL_DEBUG)
        self.assertEqual(values["DB_ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(values["DB_NAME"], "open")

    def test_full_url_with_password_loads(self) -> None:
        values = _settings(
            {**PRODUCTION_LIKE, "DATABASE_URL": "postgres://user:pass@host:5432/db"}
        )
        self.assertEqual(values["DB_ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(values["DB_NAME"], "db")
        self.assertEqual(values["DB_USER"], "user")
        self.assertEqual(values["DB_HOST"], "host")
        self.assertEqual(values["DB_PORT"], "5432")
