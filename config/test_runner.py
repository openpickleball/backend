"""Test runner for `manage.py test`.

It serves the suite over plain http and swaps the cache for an in-memory
one, so the tests need no cache server.
"""

from typing import Any

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

# With DEBUG off, settings turn on SECURE_SSL_REDIRECT, so every test
# client request would get a 301 to https. Tests run over plain http.
# Settings point the cache at Valkey. Tests use LocMemCache instead.
_TEST_OVERRIDES: dict[str, Any] = {
    "SECURE_SSL_REDIRECT": False,
    "CACHES": {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
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
    """Run the suite over plain http and with an in-memory cache.

    This turns the redirect off and swaps the cache for the test run only,
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
