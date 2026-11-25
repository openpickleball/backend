"""migrate runs on the direct connection in pooled mode (ADR 0038).

With --database not given, the command points the default connection at
the direct database for its own process, then migrates default. An
explicit --database is honored as given.
"""

from argparse import Namespace
from typing import Any
from unittest import mock

import pytest
from django.core.management import call_command, get_commands
from django.core.management.base import BaseCommand
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


def _run(databases: dict[str, Any], *argv: str) -> tuple[list[str], int]:
    """Run migrate with the real work mocked: databases used, rebinds."""
    calls: list[str] = []

    def fake_execute(self: BaseCommand, *args: Any, **options: Any) -> None:
        calls.append(options["database"])

    with (
        override_settings(DATABASES=databases),
        mock.patch.object(BaseCommand, "execute", fake_execute),
        mock.patch(
            "apps.database.management.commands.migrate.point_default_at_direct"
        ) as rebind,
    ):
        if argv and argv[0] == "manage.py":
            Command().run_from_argv([*argv[:1], "migrate", *argv[1:]])
        else:
            call_command("migrate", *argv, verbosity=0)
    return calls, rebind.call_count


def test_command_line_rebinds_in_pooled_mode() -> None:
    assert _run(POOLED_DATABASES, "manage.py") == (["default"], 1)


def test_command_line_with_explicit_database_never_rebinds() -> None:
    # --database choices come from the test run's own connections, which
    # hold no direct alias; rebinds_default covers an explicit direct.
    calls = _run(POOLED_DATABASES, "manage.py", "--database", "default")
    assert calls == (["default"], 0)


def test_unpooled_command_line_migrates_default() -> None:
    assert _run(UNPOOLED_DATABASES, "manage.py") == (["default"], 0)


def test_unpooled_call_command_migrates_default() -> None:
    assert _run(UNPOOLED_DATABASES) == (["default"], 0)
