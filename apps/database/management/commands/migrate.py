"""migrate, on the direct connection when DATABASE_URL is a pool (ADR 0038).

In pooled mode (DIRECT_DATABASE_URL set) settings add a ``direct``
database alias. A schema change should not go through a pool in
transaction mode, so this command makes ``direct`` the default for
``--database``. An explicit ``--database`` still wins, and the test
runner's own migrate calls name their database. Without a pool there is
no ``direct`` alias and nothing changes.
"""

from django.conf import settings
from django.core.management.base import CommandParser
from django.core.management.commands import migrate

DIRECT_ALIAS = "direct"


class Command(migrate.Command):
    def add_arguments(self, parser: CommandParser) -> None:
        super().add_arguments(parser)
        if DIRECT_ALIAS in settings.DATABASES:
            parser.set_defaults(database=DIRECT_ALIAS)
            for action in parser._actions:
                if action.dest == "database":
                    action.help = (
                        "Nominates a database to synchronize. Defaults to the "
                        '"direct" database (DIRECT_DATABASE_URL).'
                    )
