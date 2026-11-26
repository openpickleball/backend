"""allauth's OpenAPI document, with OPEN's own headless paths (issue #223).

allauth builds its document from a fixed file and offers no hook to add a
path, so OPEN's views for ``/_allauth/openapi.json`` and ``.yaml``
(``apps/accounts/headless_views.py``) serve allauth's document with the
signup age check (ADR 0020) and password check (ADR 0039) added, and
with OPEN's account security changes (#100, ADR 0047): sessions by
opaque string ids, the app client's ``X-Device-Token`` on login and the
MFA login stage, and login's 429. This uses allauth's internal
``get_schema``: check it when upgrading allauth (a test pins the result).
"""

import copy
from typing import Any

from allauth.headless import app_settings
from allauth.headless.spec.internal.schema import get_schema
from django.urls import reverse

from apps.players.models import EARLIEST_BIRTH_YEAR

AGE_CHECK_PATH = "{client}/v1/auth/signup/age-check"
PASSWORD_CHECK_PATH = "{client}/v1/auth/signup/password-check"

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


_PASSWORD_DESCRIPTION = """\
Checks only a password, with signup's password rules, before the app asks
for the age or any other detail. The email address is optional and is used
only to check that the password is not too similar to it: it is never
validated or looked up, so the answer is the same whether or not an
account has it.

A password that passes gets a success answer. One that fails gets a 400
with one error per failed rule, each with `param` `password` and Django's
message and code (`password_too_short`, `password_too_common`,
`password_entirely_numeric`, `password_too_similar`,
`password_breached` for a password found in a data breach, and
`password_invalid_characters` for one that is not UTF-8 text), the same errors
signup gives for that password and address. A missing, empty, or
non-string password gets a field error on `password`.

Nothing is written to the session and no email is sent. A session whose
signup was refused gets signup's fixed refusal (`signup_refused`). The
check counts against signup's rate limit.
"""

_PASSWORD_ERRORS_EXAMPLE = {
    "status": 400,
    "errors": [
        {
            "message": "This password is too common.",
            "code": "password_too_common",
            "param": "password",
        },
        {
            "message": "This password is entirely numeric.",
            "code": "password_entirely_numeric",
            "param": "password",
        },
    ],
}


def _common_responses(bad_request: dict[str, Any]) -> dict[str, Any]:
    """The checks' responses: 200, the given 400, allauth's 403 and 409,
    and the rate limit's 429."""
    return {
        "200": {"$ref": "#/components/responses/StatusOK"},
        "400": bad_request,
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
    }


def _password_check_operation(pinned: bool) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "tags": ["Authentication: Account"],
        "summary": "Check the signup password",
        "description": _PASSWORD_DESCRIPTION,
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "email": {
                                "type": "string",
                                "description": (
                                    "Optional. Used only for the similarity "
                                    "rule, never validated or looked up."
                                ),
                            },
                            "password": {"type": "string"},
                        },
                        "required": ["password"],
                    }
                }
            },
        },
        "responses": _common_responses(
            {
                "description": (
                    "One error per failed password rule, a missing or "
                    "malformed password (a field error), or the fixed "
                    "refusal for a session whose signup was refused."
                ),
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                        "examples": {
                            "password_rules": {"value": _PASSWORD_ERRORS_EXAMPLE},
                            "signup_refused": {"value": _REFUSAL_EXAMPLE},
                        },
                    }
                },
            }
        ),
    }
    if not pinned:
        operation["parameters"] = [{"$ref": "#/components/parameters/Client"}]
    return operation


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
        "responses": _common_responses(
            {
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
            }
        ),
    }
    if not pinned:
        operation["parameters"] = [{"$ref": "#/components/parameters/Client"}]
    return operation


_DEVICE_TOKEN = "X-Device-Token"
_DEVICE_TOKEN_DESCRIPTION = """\
App client only. The device token the last completed login on this
install returned, if any. Send it on every request of a login (this one
and the MFA step), so a sign-in from a known install sends no new
sign-in email. The browser client uses a cookie instead.
"""
_DEVICE_TOKEN_RETURNED = """\
App client only, on a completed login: the device token to keep and send
with the next login. It replaces any earlier one.
"""
_SESSION_ID = {
    "type": "string",
    "description": (
        "An opaque id for the session, from the session list. Never a row id."
    ),
    "example": "3f9a1c0e8b7d4a2f9c6e5d4b3a291807",
}


def _add_account_security(spec: dict[str, Any], root: str, pinned: bool) -> None:
    """Sessions by string ids, the device token, and login's 429 (#100)."""
    schemas = spec["components"]["schemas"]
    schemas["Session"]["properties"]["id"] = dict(_SESSION_ID)
    schemas["EndSessions"]["properties"]["sessions"]["items"] = dict(_SESSION_ID)
    client = "{client}" if not pinned else next(iter(app_settings.CLIENTS))
    for endpoint in ("auth/login", "auth/2fa/authenticate"):
        operation = spec["paths"][f"{root}/{client}/v1/{endpoint}"]["post"]
        operation.setdefault("parameters", []).append(
            {
                "name": _DEVICE_TOKEN,
                "in": "header",
                "required": False,
                "description": _DEVICE_TOKEN_DESCRIPTION,
                "schema": {"type": "string"},
            }
        )
        ok = operation["responses"]["200"]
        if "$ref" in ok:
            # A $ref takes no sibling keys: copy the response it names.
            name = ok["$ref"].rpartition("/")[2]
            ok = copy.deepcopy(spec["components"]["responses"][name])
        ok["headers"] = {
            _DEVICE_TOKEN: {
                "description": _DEVICE_TOKEN_RETURNED,
                "schema": {"type": "string"},
            }
        }
        operation["responses"]["200"] = ok
    # The failed login lockout answers 429 on each (apps/accounts/lockout.py).
    for endpoint in (
        "auth/login",
        "auth/2fa/authenticate",
        "auth/reauthenticate",
        "auth/2fa/reauthenticate",
    ):
        operation = spec["paths"][f"{root}/{client}/v1/{endpoint}"]["post"]
        operation["responses"]["429"] = {
            "$ref": "#/components/responses/TooManyRequests"
        }


def open_schema() -> dict[str, Any]:
    """allauth's document, with the signup age and password checks for
    each client and OPEN's account security changes."""
    spec: dict[str, Any] = get_schema()
    clients = list(app_settings.CLIENTS)
    # The headless root, as allauth's own paths use it.
    root = reverse("headless:openapi_yaml").rpartition("/")[0]
    # allauth names the client in the path, or pins it when only one is on.
    pinned = len(clients) == 1
    for template, operation in (
        (AGE_CHECK_PATH, _age_check_operation(pinned)),
        (PASSWORD_CHECK_PATH, _password_check_operation(pinned)),
    ):
        path = template.replace("{client}", clients[0]) if pinned else template
        spec["paths"][f"{root}/{path}"] = {"post": operation}
    _add_account_security(spec, root, pinned)
    return spec
