"""An in-process call_command("migrate") never rebinds default (ADR 0038).

The rebind is for ``manage.py migrate`` from the command line. In a test
run's parallel workers the direct alias is not mirrored, so a rebind from
call_command would migrate the real database, and it is never undone, so
it would break the calling test's transaction.
"""

from typing import Any
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.test import override_settings

POOLED_DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "pool"},
    "direct": {"ENGINE": "django.db.backends.postgresql", "NAME": "open"},
}


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_call_command_migrates_default_without_rebinding() -> None:
    calls: list[dict[str, Any]] = []

    def fake_execute(self: BaseCommand, *args: Any, **options: Any) -> None:
        calls.append(options)

    with (
        override_settings(DATABASES=POOLED_DATABASES),
        mock.patch.object(BaseCommand, "execute", fake_execute),
        mock.patch(
            "apps.database.management.commands.migrate.point_default_at_direct"
        ) as rebind,
    ):
        call_command("migrate", verbosity=0)
    rebind.assert_not_called()
    assert [options["database"] for options in calls] == ["default"]
