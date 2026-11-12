"""Test runner for `manage.py test`."""

from typing import Any

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import override_settings

# With DEBUG off, settings turn on SECURE_SSL_REDIRECT, so every test
# client request would get a 301 to https. Tests run over plain http.
_HTTP_ONLY = {'SECURE_SSL_REDIRECT': False}


class HttpParallelTestSuite(ParallelTestSuite):
    """Apply the http override in parallel workers too.

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
        override_settings(**_HTTP_ONLY).enable()


class HttpTestRunner(DiscoverRunner):
    """Run the suite over plain http whatever DJANGO_DEBUG says.

    This turns the redirect off for the test run only, in the parent and in
    parallel workers. Production settings are unchanged.
    """

    parallel_test_suite = HttpParallelTestSuite

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        self._http_override = override_settings(**_HTTP_ONLY)
        self._http_override.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        self._http_override.disable()
        super().teardown_test_environment(**kwargs)
