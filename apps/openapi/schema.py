"""The committed OpenAPI document: where it lives and how it is rendered."""

import json
from pathlib import Path

from django.conf import settings
from ninja.responses import NinjaJSONEncoder


def schema_path() -> Path:
    """The committed document, `openapi/openapi.json` under the repository root."""
    return Path(settings.BASE_DIR) / "openapi" / "openapi.json"


def render_schema() -> str:
    """Render the API's OpenAPI document as stable JSON text.

    Sorted keys and a fixed indent make the output the same on every run and
    under every settings module, so a diff shows only real API changes.
    """
    # Imported here so loading this module does not build the API.
    from config.api import api

    document = api.get_openapi_schema()
    text = json.dumps(
        document,
        cls=NinjaJSONEncoder,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return text + "\n"
