"""Write the OpenAPI document to `openapi/openapi.json`, or check it.

django-ninja ships a command of the same name. OPEN's app is listed before
`ninja` in `INSTALLED_APPS`, so Django runs this one. Ninja's command needs
`--api` and formatting flags to give the committed form, and fails when run
bare. This one always renders `config.api.api` the same way and writes the
file under the repository root, whatever the working directory.
"""

from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.openapi.schema import render_schema, schema_path

REGENERATE = "uv run python manage.py export_openapi_schema"


class Command(BaseCommand):
    help = "Write the OpenAPI document to openapi/openapi.json, or check it."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--output",
            type=Path,
            default=None,
            help="Where to write the document (default: openapi/openapi.json).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Write nothing; fail when the file differs from the API.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        output: Path = options["output"] or schema_path()
        text = render_schema()
        name = _display_name(output)

        if options["check"]:
            if not output.is_file():
                raise CommandError(f"{name} is missing. Generate it with: {REGENERATE}")
            # Bytes, not text: reading text would fold CRLF into LF.
            if output.read_bytes() != text.encode("utf-8"):
                raise CommandError(
                    f"{name} is out of date. Regenerate it with: {REGENERATE}"
                )
            self.stdout.write(f"{name} is up to date.")
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="\n")
        self.stdout.write(f"Wrote {output}.")


def _display_name(path: Path) -> str:
    """The path relative to the repository root when it is under it."""
    try:
        return str(path.resolve().relative_to(Path(settings.BASE_DIR).resolve()))
    except ValueError:
        return str(path)
