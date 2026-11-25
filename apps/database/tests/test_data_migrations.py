"""Data migrations write through migrate's own connection (ADR 0038).

In pooled mode migrate runs on the ``direct`` alias. A RunPython function
that queries through a plain manager, or calls Django's create_permissions
without ``using``, goes to ``default`` instead: the pool. Its writes then
commit outside the migration's transaction, and fail when only the
direct connection is reachable. Each function must use
``schema_editor.connection.alias``.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import migrations
from django.db.migrations.loader import MigrationLoader
from django.db.utils import ConnectionDoesNotExist

MISSING_ALIAS = "no_such_alias"


def _run_python_functions() -> Iterator[tuple[str, Any, Any]]:
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for (app_label, name), migration in sorted(loader.disk_migrations.items()):
        if not type(migration).__module__.startswith("apps."):
            continue
        for operation in migration.operations:
            if not isinstance(operation, migrations.RunPython):
                continue
            apps = loader.project_state((app_label, name), at_end=False).apps
            for function in (operation.code, operation.reverse_code):
                if function is not None and function is not migrations.RunPython.noop:
                    yield f"{app_label}.{name}.{function.__name__}", function, apps


FUNCTIONS = list(_run_python_functions())


def test_first_party_data_migrations_are_found() -> None:
    assert len(FUNCTIONS) >= 4


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("function", "apps"),
    [(function, apps) for _, function, apps in FUNCTIONS],
    ids=[label for label, _, _ in FUNCTIONS],
)
def test_data_migration_uses_the_schema_editor_alias(function: Any, apps: Any) -> None:
    # A connection alias that does not exist: a function that uses it
    # raises before any query, and one that uses default does not raise.
    schema_editor = SimpleNamespace(connection=SimpleNamespace(alias=MISSING_ALIAS))
    with pytest.raises(ConnectionDoesNotExist):
        function(apps, schema_editor)
