"""No Django setting holds the direct database URL (ADR 0038).

An uppercase name in config/settings.py becomes a Django setting. Django's
DEBUG error page and diffsettings show settings, hiding only names that
match its sensitive pattern, which DIRECT_DATABASE_URL does not. So the
raw URL, password included, must not be stored under such a name.
"""

import json

from config.tests.test_settings import PRODUCTION_LIKE, _run

DB_MARKER = "not-a-real-direct-pw-MARKER"

POOLED = {
    **PRODUCTION_LIKE,
    "DATABASE_URL": "postgres://open@pool.example.org:25061/open-pool",
    "DIRECT_DATABASE_URL": f"postgres://open:{DB_MARKER}@db.example.org:25060/open",
}

# DATABASES holds the password by design, as for DATABASE_URL; Django's
# filter hides it there (the PASSWORD key matches its pattern).
PRINT_SETTINGS = f"""
import json
import django
from django.conf import settings
from django.views.debug import SafeExceptionReporterFilter
django.setup()
leaks = [
    name for name in dir(settings)
    if name.isupper() and name != 'DATABASES'
    and {DB_MARKER!r} in repr(getattr(settings, name))
]
safe = repr(SafeExceptionReporterFilter().get_safe_settings())
print(json.dumps({{
    'HAS_SETTING': hasattr(settings, 'DIRECT_DATABASE_URL'),
    'LEAKS': leaks,
    'SAFE_LEAKS': {DB_MARKER!r} in safe,
    'POOLED': 'direct' in settings.DATABASES,
}}))
"""


def _inspect() -> dict[str, object]:
    result = _run(["-c", PRINT_SETTINGS], POOLED)
    assert result.returncode == 0, result.stderr
    values: dict[str, object] = json.loads(result.stdout)
    return values


def test_no_setting_holds_the_direct_url() -> None:
    values = _inspect()
    assert values["POOLED"] is True
    assert values["HAS_SETTING"] is False
    assert values["LEAKS"] == []


def test_safe_settings_never_show_the_direct_password() -> None:
    assert _inspect()["SAFE_LEAKS"] is False
