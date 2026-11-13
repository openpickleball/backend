"""Settings for the pytest run.

pytest loads this module (see [tool.pytest.ini_options] in pyproject.toml).
It applies the same overrides as config/test_runner.py, the `manage.py test`
counterpart, and nothing else: tests run over plain http, with an
in-memory cache, with Celery tasks run eagerly in the caller, and with
plain static file storage. The overrides live in the settings module, so
every process that loads it, parallel workers included, gets them. A test
fails if the two sets of overrides drift apart.
"""

from config.settings import *  # noqa: F403

# With DEBUG off, settings turn on the https redirect. Tests use plain http.
SECURE_SSL_REDIRECT = False

# Settings point the cache at Valkey. Tests use an in-memory cache instead.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Settings point Celery at a Valkey broker. Tests run each task eagerly in
# the caller, raising its errors, so no broker is needed.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Settings store static files with hashed names from a manifest that
# collectstatic writes. Tests use the plain storage, so no collectstatic.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
