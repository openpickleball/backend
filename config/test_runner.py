"""Test runner for `manage.py test`.

It serves the suite over plain http, swaps the cache for an in-memory
one, runs Celery tasks eagerly, stores static files without the
collectstatic manifest, hashes passwords with a fast hasher, serves the
admin at admin/, and encrypts personal data fields with a key generated
when this module is imported, so the tests need no cache server, broker,
collectstatic step, or stored key.
"""

from typing import Any

from cryptography.fernet import Fernet
from django.conf import global_settings, settings
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

# With DEBUG off, settings turn on SECURE_SSL_REDIRECT, so every test
# client request would get a 301 to https. Tests run over plain http.
# Settings point the cache at Valkey. Tests use LocMemCache instead.
# Settings point Celery at a Valkey broker. Tests run tasks eagerly.
# Settings store static files with hashed names from a manifest that
# collectstatic writes. Tests use the plain storage instead.
# Settings hash passwords with production-strength PBKDF2. Tests hash with
# fast MD5 first and keep the defaults, so identify_hasher still knows them.
# A developer's .env may give the admin a private path. Tests use admin/.
# Personal data fields need an encryption key (ADR 0046). Each process
# generates its own when it imports this module, so no key is ever
# stored; config/settings_test.py reads this one, so the two sets match.
_TEST_OVERRIDES: dict[str, Any] = {
    "SECURE_SSL_REDIRECT": False,
    "ADMIN_URL": "admin/",
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    "CELERY_TASK_ALWAYS_EAGER": True,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "STORAGES": {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    },
    "PASSWORD_HASHERS": [
        "django.contrib.auth.hashers.MD5PasswordHasher",
        *global_settings.PASSWORD_HASHERS,
    ],
    "FIELD_ENCRYPTION_KEYS": [Fernet.generate_key().decode()],
}


class HttpParallelTestSuite(ParallelTestSuite):
    """Apply the test overrides in parallel workers too.

    Workers started with spawn (the default on macOS and Windows) import
    settings afresh, so the parent's override does not reach them. Django
    calls process_setup in each such worker before django.setup().
    """

    # Django calls this as a plain function with process_setup_args, so it
    # takes no self.
    def process_setup(*args: Any) -> None:
        # Load settings first. It runs before django.setup(), and enabling an
        # override on settings not yet loaded would hide all of them.
        settings.SECURE_SSL_REDIRECT  # noqa: B018
        override_settings(**_TEST_OVERRIDES).enable()


class HttpTestRunner(DiscoverRunner):
    """Run the suite over plain http, an in-memory cache, and eager tasks.

    This turns the redirect off, swaps the cache, runs Celery tasks
    eagerly, and swaps the static files storage for the test run only,
    in the parent and in parallel workers. Production settings are
    unchanged.
    """

    parallel_test_suite = HttpParallelTestSuite

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        self._test_override = override_settings(**_TEST_OVERRIDES)
        self._test_override.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        self._test_override.disable()
        super().teardown_test_environment(**kwargs)
