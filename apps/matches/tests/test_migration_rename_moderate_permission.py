"""The data migration renames the stored ``moderate_match`` permission (#273).

``AlterModelOptions`` changes only the model state, and Django never renames
a stored permission, so migration 0016 renames the row. The test runs
inside the test transaction, which rolls back, and leaves the schema at the
latest migration again.
"""

from collections.abc import Iterator

import pytest
from django.contrib.auth.models import Permission
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

pytestmark = pytest.mark.django_db

BEFORE_RENAME = [("matches", "0015_alter_match_options")]
AFTER_RENAME = [("matches", "0016_rename_moderate_match_permission")]
NEW_NAME = "Can void matches in bulk"
OLD_NAME = "Can void matches and resolve disputes"


@pytest.fixture
def executor() -> Iterator[MigrationExecutor]:
    """A migration executor that leaves the schema at the latest migration."""
    migrator = MigrationExecutor(connection)
    yield migrator
    migrator.loader.build_graph()
    migrator.migrate(migrator.loader.graph.leaf_nodes())


def _permission() -> Permission:
    return Permission.objects.get(
        content_type__app_label="matches", codename="moderate_match"
    )


def test_the_migrated_test_database_has_the_new_name() -> None:
    assert _permission().name == NEW_NAME


def test_the_migration_renames_an_old_row_and_reverses(
    executor: MigrationExecutor,
) -> None:
    executor.migrate(BEFORE_RENAME)
    Permission.objects.filter(pk=_permission().pk).update(name=OLD_NAME)

    executor.loader.build_graph()
    executor.migrate(AFTER_RENAME)
    assert _permission().name == NEW_NAME

    executor.loader.build_graph()
    executor.migrate(BEFORE_RENAME)
    assert _permission().name == OLD_NAME


def test_the_migration_does_nothing_without_the_row(
    executor: MigrationExecutor,
) -> None:
    executor.migrate(BEFORE_RENAME)
    Permission.objects.filter(pk=_permission().pk).delete()

    executor.loader.build_graph()
    executor.migrate(AFTER_RENAME)

    assert not Permission.objects.filter(
        content_type__app_label="matches", codename="moderate_match"
    ).exists()
