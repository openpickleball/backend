"""migrate runs on the direct connection in pooled mode (ADR 0038).

With --database not given, the command points the default connection at
the direct database for its own process, then migrates default. An
explicit --database is honored as given.
"""

from argparse import Namespace

import pytest
from django.core.management import get_commands
from django.db.utils import ConnectionHandler
from django.test import override_settings

from apps.database.management.commands.migrate import (
    Command,
    point_default_at_direct,
    rebinds_default,
)

POOLED_DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "pool"},
    "direct": {"ENGINE": "django.db.backends.postgresql", "NAME": "open"},
}
UNPOOLED_DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "open"},
}

pytestmark = pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")


def _parse(*args: str) -> Namespace:
    return Command().create_parser("manage.py", "migrate").parse_args(list(args))


def test_open_command_replaces_django_migrate() -> None:
    assert get_commands()["migrate"] == "apps.database"


def test_database_is_none_when_not_given() -> None:
    assert _parse().database is None
    assert _parse("--database", "default").database == "default"


def test_pooled_mode_rebinds_when_database_is_not_given() -> None:
    with override_settings(DATABASES=POOLED_DATABASES):
        assert rebinds_default(None) is True


@pytest.mark.parametrize("database", ["default", "direct"])
def test_explicit_database_never_rebinds(database: str) -> None:
    with override_settings(DATABASES=POOLED_DATABASES):
        assert rebinds_default(database) is False


def test_unpooled_mode_never_rebinds() -> None:
    with override_settings(DATABASES=UNPOOLED_DATABASES):
        assert rebinds_default(None) is False


def test_rebind_points_default_at_direct_and_keeps_its_test_settings() -> None:
    handler = ConnectionHandler(
        {
            "default": {**POOLED_DATABASES["default"], "TEST": {"NAME": "test_x"}},
            "direct": {**POOLED_DATABASES["direct"], "TEST": {"MIRROR": "default"}},
        }
    )
    pool_wrapper = handler["default"]
    point_default_at_direct(handler)
    rebound = handler["default"]
    assert rebound is not pool_wrapper
    assert rebound.settings_dict["NAME"] == "open"
    assert rebound.settings_dict["TEST"]["NAME"] == "test_x"
    assert rebound.settings_dict["TEST"].get("MIRROR") is None
    # The direct alias itself is unchanged.
    assert handler["direct"].settings_dict["NAME"] == "open"
