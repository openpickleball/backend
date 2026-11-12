"""Settings for the pytest run.

pytest loads this module (see [tool.pytest.ini_options] in pyproject.toml).
It applies the same overrides as config/test_runner.py, the `manage.py test`
counterpart, and nothing else: tests run over plain http and with an
in-memory cache. The overrides live in the settings module, so every
process that loads it, parallel workers included, gets them. A test fails
if the two sets of overrides drift apart.
"""

from config.settings import *  # noqa: F403

# With DEBUG off, settings turn on the https redirect. Tests use plain http.
SECURE_SSL_REDIRECT = False

# Settings point the cache at Valkey. Tests use an in-memory cache instead.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
