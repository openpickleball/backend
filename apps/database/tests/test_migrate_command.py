"""migrate runs on the direct connection in pooled mode (ADR 0038)."""

from argparse import Namespace

import pytest
from django.core.management import get_commands
from django.test import override_settings

from apps.database.management.commands.migrate import Command

POOLED_DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "pool"},
    "direct": {"ENGINE": "django.db.backends.postgresql", "NAME": "open"},
}
UNPOOLED_DATABASES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "open"},
}


def _parse(*args: str) -> Namespace:
    return Command().create_parser("manage.py", "migrate").parse_args(list(args))


def test_open_command_replaces_django_migrate() -> None:
    assert get_commands()["migrate"] == "apps.database"


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_pooled_mode_migrates_on_direct_by_default() -> None:
    with override_settings(DATABASES=POOLED_DATABASES):
        assert _parse().database == "direct"


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_explicit_database_wins_in_pooled_mode() -> None:
    with override_settings(DATABASES=POOLED_DATABASES):
        assert _parse("--database", "default").database == "default"


@pytest.mark.filterwarnings("ignore:Overriding setting DATABASES")
def test_unpooled_mode_migrates_on_default() -> None:
    with override_settings(DATABASES=UNPOOLED_DATABASES):
        assert _parse().database == "default"
