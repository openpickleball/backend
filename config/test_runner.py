"""Test runner for `manage.py test`."""

from typing import Any

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class HttpTestRunner(DiscoverRunner):
    """Run the suite over plain http whatever DJANGO_DEBUG says.

    With DEBUG off, settings turn on SECURE_SSL_REDIRECT, so every test
    client request would get a 301 to https. This turns the redirect off
    for the test run only. Production settings are unchanged.
    """

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        self._http_override = override_settings(SECURE_SSL_REDIRECT=False)
        self._http_override.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        self._http_override.disable()
        super().teardown_test_environment(**kwargs)
