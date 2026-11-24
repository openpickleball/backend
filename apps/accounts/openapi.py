"""allauth's OpenAPI document, with OPEN's own headless paths (issue #223).

allauth builds its document from a fixed file and offers no hook to add a
path, so OPEN's views for ``/_allauth/openapi.json`` and ``.yaml``
(``apps/accounts/headless_views.py``) serve allauth's document with the
signup age check added (ADR 0020). This uses allauth's internal
``get_schema``: check it when upgrading allauth (a test pins the result).
"""

from typing import Any

from allauth.headless import app_settings
from allauth.headless.spec.internal.schema import get_schema
from django.urls import reverse

from apps.players.models import EARLIEST_BIRTH_YEAR

AGE_CHECK_PATH = "{client}/v1/auth/signup/age-check"

_DESCRIPTION = """\
Checks only a birth year and month, with the signup age screen's rules,
before the app asks for any other personal detail (a name or a location).
It reads no email address.

13 or older gets a success answer and the session keeps nothing: the
signup request still sends and checks the age itself. Under 13 gets the
same fixed refusal as signup (`signup_refused`), and the session is
flagged, so this check and every later signup in it get that refusal,
whatever age they send. A missing or malformed value gets a field error.

The check counts against signup's rate limit.
"""

_REFUSAL_EXAMPLE = {
    "status": 400,
    "errors": [
        {
            "message": "We can't create an account with these details.",
            "code": "signup_refused",
        }
    ],
}


def _age_check_operation(pinned: bool) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "tags": ["Authentication: Account"],
        "summary": "Check the signup age",
        "description": _DESCRIPTION,
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "birth_year": {
                                "type": "integer",
                                "minimum": EARLIEST_BIRTH_YEAR,
                            },
                            "birth_month": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 12,
                            },
                        },
                        "required": ["birth_year", "birth_month"],
                    }
                }
            },
        },
        "responses": {
            "200": {"$ref": "#/components/responses/StatusOK"},
            "400": {
                "description": (
                    "A missing or malformed value (a field error), or the "
                    "fixed refusal for an age under 13 or a flagged session."
                ),
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                        "examples": {"signup_refused": {"value": _REFUSAL_EXAMPLE}},
                    }
                },
            },
            "403": {
                "description": "Forbidden, when signup is closed.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ForbiddenResponse"}
                    }
                },
            },
            "409": {
                "description": "Conflict, when the session is signed in.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ConflictResponse"}
                    }
                },
            },
            "429": {"$ref": "#/components/responses/TooManyRequests"},
        },
    }
    if not pinned:
        operation["parameters"] = [{"$ref": "#/components/parameters/Client"}]
    return operation


def open_schema() -> dict[str, Any]:
    """allauth's document, with the signup age check for each client."""
    spec: dict[str, Any] = get_schema()
    clients = list(app_settings.CLIENTS)
    # The headless root, as allauth's own paths use it.
    root = reverse("headless:openapi_yaml").rpartition("/")[0]
    # allauth names the client in the path, or pins it when only one is on.
    pinned = len(clients) == 1
    path = AGE_CHECK_PATH.replace("{client}", clients[0]) if pinned else AGE_CHECK_PATH
    spec["paths"][f"{root}/{path}"] = {"post": _age_check_operation(pinned)}
    return spec
