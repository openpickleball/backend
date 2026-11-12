"""Tests for environment-based settings (issue #4).

Settings are read once at import, so each case starts a fresh Python
process with a fully controlled environment. Every variable the settings
read is set explicitly in every case ('' means absent). Values in the
process environment win over a .env file, so a developer's local .env
cannot leak into these tests.
"""

import json
import os
import ssl
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SETTINGS_VARS = (
    "DJANGO_SECRET_KEY",
    "DJANGO_DEBUG",
    "DJANGO_ALLOWED_HOSTS",
    "DATABASE_URL",
    "REDIS_URL",
    "CSRF_TRUSTED_ORIGINS",
)

# Made-up values for tests only. Not a real secret.
TEST_SECRET_KEY = "test-only-" + "k3y" * 16

PRODUCTION_LIKE = {
    "DJANGO_SECRET_KEY": TEST_SECRET_KEY,
    "DJANGO_DEBUG": "False",
    "DJANGO_ALLOWED_HOSTS": "api.example.org,www.example.org",
    "DATABASE_URL": "postgres://open@db.example.org:5432/open",
    "REDIS_URL": "rediss://cache.example.org:6379/0",
    "CSRF_TRUSTED_ORIGINS": "https://api.example.org",
}

LOCAL_DEBUG = {
    "DJANGO_SECRET_KEY": "",
    "DJANGO_DEBUG": "True",
    "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
    "DATABASE_URL": "postgres://open@localhost:5432/open",
    "REDIS_URL": "redis://localhost:6379/0",
    "CSRF_TRUSTED_ORIGINS": "",
}

PRINT_SETTINGS = """
import json
import django
from django.conf import settings
django.setup()
db = settings.DATABASES['default']
print(json.dumps({
    'DEBUG': settings.DEBUG,
    'SECRET_KEY': settings.SECRET_KEY,
    'ALLOWED_HOSTS': settings.ALLOWED_HOSTS,
    'CSRF_TRUSTED_ORIGINS': settings.CSRF_TRUSTED_ORIGINS,
    'REDIS_URL': settings.REDIS_URL,
    'DB_ENGINE': db['ENGINE'],
    'DB_NAME': db['NAME'],
    'DB_USER': db['USER'],
    'DB_HOST': db['HOST'],
    'DB_PORT': str(db['PORT']),
    'SECURE_PROXY_SSL_HEADER': settings.SECURE_PROXY_SSL_HEADER,
    'SECURE_SSL_REDIRECT': settings.SECURE_SSL_REDIRECT,
    'SESSION_COOKIE_SECURE': settings.SESSION_COOKIE_SECURE,
    'CSRF_COOKIE_SECURE': settings.CSRF_COOKIE_SECURE,
    'SECURE_HSTS_SECONDS': settings.SECURE_HSTS_SECONDS,
    'SECURE_HSTS_INCLUDE_SUBDOMAINS': settings.SECURE_HSTS_INCLUDE_SUBDOMAINS,
    'SECURE_HSTS_PRELOAD': settings.SECURE_HSTS_PRELOAD,
    'CACHE_BACKEND': settings.CACHES['default']['BACKEND'],
    'CACHE_LOCATION': settings.CACHES['default'].get('LOCATION'),
    'CACHE_KEY_PREFIX': settings.CACHES['default'].get('KEY_PREFIX'),
    'CACHE_TIMEOUT': settings.CACHES['default'].get('TIMEOUT'),
}))
"""

# Builds the cache's connection pool without connecting to a server.
PRINT_CACHE_POOL = """
import json
import django
from django.core.cache import caches
django.setup()
pool = caches['default']._cache._get_connection_pool(write=True)
# Constructing a connection object does not connect.
connection = pool.connection_class(**pool.connection_kwargs)
print(json.dumps({
    'CONNECTION_CLASS': pool.connection_class.__name__,
    'CERT_REQS': int(getattr(connection, 'cert_reqs', -1)),
    'CHECK_HOSTNAME': getattr(connection, 'check_hostname', None),
    'SERVERS': len(caches['default']._servers),
}))
"""

# A made-up password that must never appear in any output.
REDIS_MARKER = "not-a-real-pw-MARKER"

REJECTED_REDIS_URLS = (
    f"http://u:{REDIS_MARKER}@cache.example.org:6379/0",
    f"unix://u:{REDIS_MARKER}@/tmp/valkey.sock",
    f"postgres://u:{REDIS_MARKER}@cache.example.org/db",
    f"u:{REDIS_MARKER}@cache.example.org:6379",
    "redis://",
    f"redis://u:{REDIS_MARKER}@",
    f"redis://u:{REDIS_MARKER}@cache.example.org:notaport/0",
    f"redis://u:{REDIS_MARKER}@cache.example.org:99999/0",
    # redis-py matches the scheme case-sensitively and fails on first use.
    f"REDIS://u:{REDIS_MARKER}@cache.example.org:6379/0",
    f"Rediss://u:{REDIS_MARKER}@cache.example.org:6379/0",
    # Query arguments override settings, TLS verification included.
    f"rediss://u:{REDIS_MARKER}@cache.example.org:6379/0?ssl_cert_reqs=none",
    f"rediss://u:{REDIS_MARKER}@cache.example.org:6379/0?ssl_check_hostname=false",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0?socket_timeout=0",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0?foo=bar",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0?",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0#frag",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0#",
    # redis-py reads port 0 as unset and a non-numeric db as 0.
    f"redis://u:{REDIS_MARKER}@cache.example.org:0/0",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/abc",
    f"redis://u:{REDIS_MARKER}@cache.example.org:6379/0/1",
)

ACCEPTED_REDIS_URLS = (
    "redis://localhost:6379/0",
    "redis://localhost:6379",
    "redis://localhost:6379/",
    "redis://localhost",
    "rediss://u:p@cache.example.org:25061/0",
)


def _env(values: dict[str, str]) -> dict[str, str]:
    """Build a subprocess environment that sets every settings variable."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    for name in SETTINGS_VARS:
        env[name] = values.get(name, "")
    return env


def _run(args: list[str], values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
        env=_env(values),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _check(values: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    return _run(["manage.py", "check", *extra], values)


def _settings(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_SETTINGS], values)
    if result.returncode != 0:
        raise AssertionError(f"settings failed to load:\n{result.stderr}")
    settings: dict[str, object] = json.loads(result.stdout)
    return settings


def _cache_pool(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_CACHE_POOL], values)
    if result.returncode != 0:
        raise AssertionError(f"cache pool failed to build:\n{result.stderr}")
    pool: dict[str, object] = json.loads(result.stdout)
    return pool


def _env_example() -> dict[str, str]:
    """Parse simple KEY=VALUE lines from .env.example."""
    values: dict[str, str] = {}
    for line in (BASE_DIR / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip()
    return values


class SecretKeyTests(SimpleTestCase):
    def test_debug_off_without_key_fails_naming_the_variable(self) -> None:
        result = _check({**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_debug_unset_defaults_off_and_requires_key(self) -> None:
        result = _check(
            {**PRODUCTION_LIKE, "DJANGO_SECRET_KEY": "", "DJANGO_DEBUG": ""}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_debug_false_string_means_off(self) -> None:
        values = _settings({**PRODUCTION_LIKE, "DJANGO_DEBUG": "false"})
        self.assertIs(values["DEBUG"], False)

    def test_debug_on_without_key_uses_labeled_development_key(self) -> None:
        result = _check(LOCAL_DEBUG)
        self.assertEqual(result.returncode, 0, result.stderr)
        values = _settings(LOCAL_DEBUG)
        self.assertIs(values["DEBUG"], True)
        self.assertTrue(str(values["SECRET_KEY"]).startswith("django-insecure-"))

    def test_provided_key_is_used(self) -> None:
        values = _settings(PRODUCTION_LIKE)
        self.assertEqual(values["SECRET_KEY"], TEST_SECRET_KEY)

    def test_error_never_echoes_the_secret_key(self) -> None:
        result = _check({**PRODUCTION_LIKE, "DATABASE_URL": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(TEST_SECRET_KEY, result.stderr)
        self.assertNotIn(TEST_SECRET_KEY, result.stdout)


class ServiceUrlTests(SimpleTestCase):
    def test_empty_database_url_fails_naming_the_variable(self) -> None:
        result = _check({**PRODUCTION_LIKE, "DATABASE_URL": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("DATABASE_URL", result.stderr)

    def test_empty_redis_url_fails_naming_the_variable(self) -> None:
        result = _check({**PRODUCTION_LIKE, "REDIS_URL": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("REDIS_URL", result.stderr)

    def test_database_url_is_parsed(self) -> None:
        values = _settings(LOCAL_DEBUG)
        self.assertEqual(values["DB_ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(values["DB_NAME"], "open")
        self.assertEqual(values["DB_USER"], "open")
        self.assertEqual(values["DB_HOST"], "localhost")
        self.assertEqual(values["DB_PORT"], "5432")

    def test_redis_url_is_stored(self) -> None:
        values = _settings(PRODUCTION_LIKE)
        self.assertEqual(values["REDIS_URL"], "rediss://cache.example.org:6379/0")


class CacheTests(SimpleTestCase):
    def test_cache_uses_redis_with_prefix_and_timeout(self) -> None:
        for env in (PRODUCTION_LIKE, LOCAL_DEBUG):
            with self.subTest(redis_url=env["REDIS_URL"]):
                values = _settings(env)
                self.assertEqual(
                    values["CACHE_BACKEND"],
                    "django.core.cache.backends.redis.RedisCache",
                )
                self.assertEqual(values["CACHE_LOCATION"], [env["REDIS_URL"]])
                self.assertEqual(values["CACHE_KEY_PREFIX"], "open")
                self.assertEqual(values["CACHE_TIMEOUT"], 300)

    def test_redis_scheme_uses_plain_connection(self) -> None:
        values = _cache_pool({**LOCAL_DEBUG, "REDIS_URL": "redis://localhost:6379/0"})
        self.assertEqual(values["CONNECTION_CLASS"], "Connection")

    def test_rediss_scheme_uses_tls_with_verification(self) -> None:
        values = _cache_pool(
            {**PRODUCTION_LIKE, "REDIS_URL": "rediss://cache.example.org:25061/0"}
        )
        self.assertEqual(values["CONNECTION_CLASS"], "SSLConnection")
        self.assertEqual(values["CERT_REQS"], int(ssl.CERT_REQUIRED))
        self.assertIs(values["CHECK_HOSTNAME"], True)

    def test_valid_redis_urls_load(self) -> None:
        for url in ACCEPTED_REDIS_URLS:
            with self.subTest(url=url):
                values = _cache_pool({**PRODUCTION_LIKE, "REDIS_URL": url})
                self.assertIn(
                    values["CONNECTION_CLASS"], ("Connection", "SSLConnection")
                )

    def test_invalid_redis_urls_fail_without_echoing_the_value(self) -> None:
        for url in REJECTED_REDIS_URLS:
            with self.subTest(url=url):
                result = _check({**PRODUCTION_LIKE, "REDIS_URL": url})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ImproperlyConfigured", result.stderr)
                self.assertIn("REDIS_URL is not a Redis URL", result.stderr)
                self.assertNotIn(REDIS_MARKER, result.stderr)
                self.assertNotIn(REDIS_MARKER, result.stdout)
                self.assertNotIn("cache.example.org", result.stderr)

    def test_separator_in_password_stays_one_server(self) -> None:
        url = "redis://u:pa,ss;wd@localhost:6379/0"
        values = _settings({**LOCAL_DEBUG, "REDIS_URL": url})
        self.assertEqual(values["CACHE_LOCATION"], [url])
        pool = _cache_pool({**LOCAL_DEBUG, "REDIS_URL": url})
        self.assertEqual(pool["SERVERS"], 1)


class ListTests(SimpleTestCase):
    def test_comma_separated_lists(self) -> None:
        values = _settings(
            {
                **PRODUCTION_LIKE,
                "DJANGO_ALLOWED_HOSTS": "a.example,b.example",
                "CSRF_TRUSTED_ORIGINS": "https://a.example,https://b.example",
            }
        )
        self.assertEqual(values["ALLOWED_HOSTS"], ["a.example", "b.example"])
        self.assertEqual(
            values["CSRF_TRUSTED_ORIGINS"], ["https://a.example", "https://b.example"]
        )

    def test_lists_default_empty(self) -> None:
        values = _settings(
            {**LOCAL_DEBUG, "DJANGO_ALLOWED_HOSTS": "", "CSRF_TRUSTED_ORIGINS": ""}
        )
        self.assertEqual(values["ALLOWED_HOSTS"], [])
        self.assertEqual(values["CSRF_TRUSTED_ORIGINS"], [])


class SecurityBlockTests(SimpleTestCase):
    def test_debug_off_sets_security_block(self) -> None:
        values = _settings(PRODUCTION_LIKE)
        self.assertEqual(
            values["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"]
        )
        self.assertIs(values["SECURE_SSL_REDIRECT"], True)
        self.assertIs(values["SESSION_COOKIE_SECURE"], True)
        self.assertIs(values["CSRF_COOKIE_SECURE"], True)
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 31536000)
        self.assertIs(values["SECURE_HSTS_INCLUDE_SUBDOMAINS"], True)
        self.assertIs(values["SECURE_HSTS_PRELOAD"], True)

    def test_debug_on_leaves_security_block_off(self) -> None:
        values = _settings(LOCAL_DEBUG)
        self.assertIsNone(values["SECURE_PROXY_SSL_HEADER"])
        self.assertIs(values["SECURE_SSL_REDIRECT"], False)
        self.assertIs(values["SESSION_COOKIE_SECURE"], False)
        self.assertIs(values["CSRF_COOKIE_SECURE"], False)
        self.assertEqual(values["SECURE_HSTS_SECONDS"], 0)

    def test_production_like_values_pass_deploy_check(self) -> None:
        result = _check(PRODUCTION_LIKE, "--deploy")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout + result.stderr
        self.assertIn("System check identified no issues (0 silenced).", output)
        self.assertNotIn("WARNINGS", output)


class EnvExampleTests(SimpleTestCase):
    def test_env_example_values_pass_check(self) -> None:
        values = _env_example()
        self.assertEqual(set(values), set(SETTINGS_VARS))
        self.assertEqual(values["DJANGO_SECRET_KEY"], "")
        result = _check(values)
        self.assertEqual(result.returncode, 0, result.stderr)
