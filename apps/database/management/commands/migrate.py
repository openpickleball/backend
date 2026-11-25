"""migrate, on the direct connection when DATABASE_URL is a pool (ADR 0038).

In pooled mode (DIRECT_DATABASE_URL set) settings add a ``direct``
database alias. A schema change should not go through a pool in
transaction mode. So when ``--database`` is not given, this command
points the ``default`` connection at the direct database for its own
process, then migrates ``default``. Every migration, data migration, and
post_migrate handler that uses the default alias then runs on the direct
connection, with no edits to any migration.

An explicit ``--database`` is honored as given and rebinds nothing: the
test runner always passes one, and ``--database default`` by hand goes
through the pool. Without a pool there is no ``direct`` alias and
nothing changes.
"""

import copy
from typing import Any

from django.conf import settings
from django.core.management.base import CommandParser
from django.core.management.commands import migrate
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.utils import ConnectionHandler

DIRECT_ALIAS = "direct"


def rebinds_default(database: str | None) -> bool:
    """Whether migrate points default at the direct database first."""
    return database is None and DIRECT_ALIAS in settings.DATABASES


def point_default_at_direct(handler: ConnectionHandler = connections) -> None:
    """Make the default alias connect to the direct database in this process.

    The default connection is closed and its cached wrapper dropped, so the
    next access builds one from the direct alias's settings. Default keeps
    its own TEST settings.
    """
    handler[DEFAULT_DB_ALIAS].close()
    rebound = copy.deepcopy(handler.settings[DIRECT_ALIAS])
    rebound["TEST"] = copy.deepcopy(handler.settings[DEFAULT_DB_ALIAS]["TEST"])
    handler.settings[DEFAULT_DB_ALIAS] = rebound
    del handler[DEFAULT_DB_ALIAS]


class Command(migrate.Command):
    help = (
        f"{migrate.Command.help} When DIRECT_DATABASE_URL is set and "
        "--database is not given, the default database is reached through "
        "DIRECT_DATABASE_URL instead of the pool."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        # None tells "not given" apart from an explicit --database default.
        parser.set_defaults(database=None)

    def execute(self, *args: Any, **options: Any) -> str | None:
        # Before execute's system checks, which connect to the database.
        if rebinds_default(options["database"]):
            point_default_at_direct()
        if options["database"] is None:
            options["database"] = DEFAULT_DB_ALIAS
        output: str | None = super().execute(*args, **options)
        return output
