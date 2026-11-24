"""The current legal agreement versions (#222, ADR 0026).

TERMS_OF_SERVICE_VERSION and PRIVACY_POLICY_VERSION are whole numbers from
1, defaulting to the versions published on openpb.org. A bad value stops
startup, naming the variable, never echoing it.
"""

import json

import pytest

from config.tests.test_settings import LOCAL_DEBUG, _run

PRINT_VERSIONS = """
import json
import django
from django.conf import settings
django.setup()
print(json.dumps([settings.TERMS_OF_SERVICE_VERSION, settings.PRIVACY_POLICY_VERSION]))
"""

TERMS = "TERMS_OF_SERVICE_VERSION"
PRIVACY = "PRIVACY_POLICY_VERSION"


def _versions(values: dict[str, str]) -> object:
    result = _run(["-c", PRINT_VERSIONS], {**LOCAL_DEBUG, **values})
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("value", ["", "  "])
def test_unset_means_the_published_versions(value: str) -> None:
    # Terms of Service "Version 1", Privacy Policy "Version 2".
    assert _versions({TERMS: value, PRIVACY: value}) == [1, 2]


@pytest.mark.parametrize(("value", "version"), [("1", 1), (" 3 ", 3), ("12", 12)])
def test_a_whole_number_sets_each_version(value: str, version: int) -> None:
    assert _versions({TERMS: value}) == [version, 2]
    assert _versions({PRIVACY: value}) == [1, version]


@pytest.mark.parametrize("name", [TERMS, PRIVACY])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "v2", "two-MARKER", "٣", "1" * 10])
def test_a_bad_value_stops_startup_without_echoing_it(name: str, value: str) -> None:
    result = _run(["manage.py", "check"], {**LOCAL_DEBUG, name: value})

    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert name in result.stderr
    assert f"={value}" not in result.stderr
    assert "MARKER" not in result.stderr
    assert "MARKER" not in result.stdout
