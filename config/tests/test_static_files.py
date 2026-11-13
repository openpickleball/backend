"""Tests for serving static files with WhiteNoise (issue #10).

Production collects static files into STATIC_ROOT with hashed, compressed
copies, and WhiteNoise serves them from the app. Tests swap in Django's
plain storage, so the suite needs no collectstatic step. The end-to-end
test here turns the production storage back on, collects into a
temporary directory, and fetches the admin CSS through the middleware.
"""

import json
import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client, override_settings

from config.tests.test_settings import LOCAL_DEBUG, PRODUCTION_LIKE, _run

BASE_DIR = Path(__file__).resolve().parent.parent.parent

WHITENOISE_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
PLAIN_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"

PRINT_STATIC_SETTINGS = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps({
    'STORAGES': settings.STORAGES,
    'STATIC_ROOT': str(settings.STATIC_ROOT),
    'MIDDLEWARE': settings.MIDDLEWARE,
}))
"""

BASE_CSS_HREF = re.compile(r'href="(/static/admin/css/base\.[0-9a-f]{12}\.css)"')


def _static_settings(values: dict[str, str]) -> dict[str, object]:
    result = _run(["-c", PRINT_STATIC_SETTINGS], values)
    assert result.returncode == 0, result.stderr
    loaded: dict[str, object] = json.loads(result.stdout)
    return loaded


@pytest.mark.parametrize("env", [PRODUCTION_LIKE, LOCAL_DEBUG])
def test_settings_use_the_whitenoise_storage(env: dict[str, str]) -> None:
    loaded = _static_settings(env)
    storages = loaded["STORAGES"]
    assert isinstance(storages, dict)
    assert storages["staticfiles"] == {"BACKEND": WHITENOISE_STORAGE}
    assert storages["default"] == {
        "BACKEND": "django.core.files.storage.FileSystemStorage"
    }


def test_static_root_is_the_staticfiles_directory() -> None:
    loaded = _static_settings(PRODUCTION_LIKE)
    assert loaded["STATIC_ROOT"] == str(BASE_DIR / "staticfiles")


def test_whitenoise_sits_directly_after_the_security_middleware() -> None:
    middleware = settings.MIDDLEWARE
    security = middleware.index("django.middleware.security.SecurityMiddleware")
    assert middleware[security + 1] == "whitenoise.middleware.WhiteNoiseMiddleware"


def test_tests_use_the_plain_storage() -> None:
    assert settings.STORAGES["staticfiles"] == {"BACKEND": PLAIN_STORAGE}


def test_collected_admin_css_is_served_hashed_and_gzipped(tmp_path: Path) -> None:
    production_storages = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": WHITENOISE_STORAGE},
    }
    with override_settings(
        STATIC_ROOT=tmp_path, STORAGES=production_storages, DEBUG=False
    ):
        call_command("collectstatic", "--noinput", verbosity=0)
        # The middleware reads STATIC_ROOT when it is built, on the first
        # request of a new client.
        client = Client()

        page = client.get("/admin/login/")
        assert page.status_code == 200
        match = BASE_CSS_HREF.search(page.content.decode())
        assert match is not None, "admin login page links no hashed base.css"
        href = match.group(1)

        plain = client.get(href)
        assert plain.status_code == 200
        assert plain["Content-Type"].startswith("text/css")
        assert "Content-Encoding" not in plain

        gzipped = client.get(href, HTTP_ACCEPT_ENCODING="gzip")
        assert gzipped.status_code == 200
        assert gzipped["Content-Type"].startswith("text/css")
        assert gzipped["Content-Encoding"] == "gzip"
