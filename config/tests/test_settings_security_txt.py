"""SECURITY_TXT_CONTACT and SECURITY_TXT_CANONICAL (issue #105).

Both are optional. Unset or empty, the contact is mailto:info@openpb.org,
the address the organization security policy names, and the canonical URL
is '' (the view builds it from the request). A contact must be a mailto:
URI or an https URL (http too with DEBUG on). A canonical URL must be an
absolute https URL (http too with DEBUG on) whose path is
/.well-known/security.txt, with no query, fragment, or user info. Neither
may hold a space or a control character. A refused value stops startup,
naming the variable and never echoing the value. Each case runs in a fresh
process. Made-up hosts only, apart from the default contact.
"""

import json
import subprocess
import sys

import pytest

from config.tests.test_settings import BASE_DIR, LOCAL_DEBUG, PRODUCTION_LIKE, _env

CONTACT = "SECURITY_TXT_CONTACT"
CANONICAL = "SECURITY_TXT_CANONICAL"
DEFAULT_CONTACT = "mailto:info@openpb.org"

# A made-up host that must never appear in any output.
MARKER = "sectxt-q7z.example.org"

PRINT_VALUES = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps([settings.SECURITY_TXT_CONTACT, settings.SECURITY_TXT_CANONICAL]))
"""


def _run(
    args: list[str], values: dict[str, str], contact: str = "", canonical: str = ""
) -> subprocess.CompletedProcess[str]:
    env = _env(values)
    # Always set, so a developer's .env never supplies them.
    env[CONTACT] = contact
    env[CANONICAL] = canonical
    return subprocess.run(
        [sys.executable, *args],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _loaded(
    values: dict[str, str], contact: str = "", canonical: str = ""
) -> list[str]:
    result = _run(["-c", PRINT_VALUES], values, contact, canonical)
    assert result.returncode == 0, result.stderr
    loaded: list[str] = json.loads(result.stdout)
    return loaded


def _refused(
    values: dict[str, str], name: str, contact: str = "", canonical: str = ""
) -> None:
    result = _run(["manage.py", "check"], values, contact, canonical)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert MARKER not in output


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize("value", ["", "   "])
def test_unset_or_empty_gives_the_default_contact_and_no_canonical(
    values: dict[str, str], value: str
) -> None:
    assert _loaded(values, value, value) == [DEFAULT_CONTACT, ""]


def test_default_contact_passes_its_own_check() -> None:
    # Set explicitly, the default is accepted with DEBUG off as well.
    assert _loaded(PRODUCTION_LIKE, contact=DEFAULT_CONTACT)[0] == DEFAULT_CONTACT


@pytest.mark.parametrize(
    "value",
    [
        "mailto:security@example.org",
        "https://example.org/report",
        "mailto:security@example.org?subject=OPEN",
        "  mailto:security@example.org  ",
    ],
)
def test_contact_with_debug_off_loads(value: str) -> None:
    assert _loaded(PRODUCTION_LIKE, contact=value)[0] == value.strip()


def test_http_contact_loads_with_debug_on() -> None:
    value = "http://localhost:8081/report"
    assert _loaded(LOCAL_DEBUG, contact=value)[0] == value


def test_http_contact_with_debug_off_stops_startup() -> None:
    _refused(PRODUCTION_LIKE, CONTACT, contact=f"http://{MARKER}/report")


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize(
    "value",
    [
        f"security@{MARKER}",
        f"ftp://{MARKER}/report",
        f"MAILTO:security@{MARKER}",
        f"mailto:security@{MARKER} extra",
        f"mailto:security@{MARKER}\nPolicy: https://{MARKER}/",
        f"mailto:security@{MARKER}\rx",
        f"mailto:sécurité@{MARKER}",
        "mailto:",
        "https://",
        "https:///report",
        f"https://@{MARKER}/",
        f"https://{MARKER}:abc/",
        f"https://{MARKER}:99999/",
        "mailto:@",
        f"mailto:@{MARKER}",
        "mailto:security@",
        "mailto:security@localhost",
    ],
)
def test_bad_contact_stops_startup_without_echoing_it(
    values: dict[str, str], value: str
) -> None:
    _refused(values, CONTACT, contact=value)


def test_canonical_with_debug_off_loads() -> None:
    value = f"https://{MARKER}/.well-known/security.txt"
    assert _loaded(PRODUCTION_LIKE, canonical=value)[1] == value


def test_http_canonical_loads_with_debug_on() -> None:
    value = "http://localhost:8000/.well-known/security.txt"
    assert _loaded(LOCAL_DEBUG, canonical=value)[1] == value


def test_http_canonical_with_debug_off_stops_startup() -> None:
    _refused(
        PRODUCTION_LIKE,
        CANONICAL,
        canonical=f"http://{MARKER}/.well-known/security.txt",
    )


@pytest.mark.parametrize("values", [PRODUCTION_LIKE, LOCAL_DEBUG])
@pytest.mark.parametrize(
    "value",
    [
        f"https://{MARKER}/",
        f"https://{MARKER}/security.txt",
        f"https://{MARKER}/.well-known/security.txt/",
        f"https://{MARKER}/.well-known/security.txt?a=1",
        f"https://{MARKER}/.well-known/security.txt?",
        f"https://{MARKER}/.well-known/security.txt#top",
        f"https://{MARKER}/.well-known/security.txt#",
        f"https://user@{MARKER}/.well-known/security.txt",
        f"ftp://{MARKER}/.well-known/security.txt",
        f"//{MARKER}/.well-known/security.txt",
        f"https://[{MARKER}/.well-known/security.txt",
        f"https://{MARKER}/.well-known/security.txt\nContact: x",
        f"https://{MARKER}:abc/.well-known/security.txt",
        f"https://{MARKER}:99999/.well-known/security.txt",
    ],
)
def test_bad_canonical_stops_startup_without_echoing_it(
    values: dict[str, str], value: str
) -> None:
    _refused(values, CANONICAL, canonical=value)
