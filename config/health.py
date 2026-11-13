"""Liveness and readiness checks, mounted at /api/v1/health/.

`live` answers as long as the process serves requests. It touches no
database and no cache, so a dependency outage never restarts the app.

`ready` checks the default database and the default cache. It answers 200
when both work and 503 when either fails, so a load balancer stops sending
traffic until the app can serve it.

Neither takes auth. A response says only which check failed, never why:
driver errors name hosts and ports. The log gets the exception class only.
Both responses carry `Cache-Control: no-store`, so no proxy serves a stale
result.
"""

import logging
from collections.abc import Callable
from typing import Literal
from uuid import uuid4

from django.core.cache import cache
from django.db import DEFAULT_DB_ALIAS, connections
from django.http import HttpRequest
from django.views.decorators.cache import never_cache
from ninja import Router, Schema, Status
from ninja.decorators import decorate_view

logger = logging.getLogger(__name__)

router = Router(tags=["health"])

CheckStatus = Literal["ok", "fail"]


class LiveOut(Schema):
    status: Literal["ok"]


class ReadyChecks(Schema):
    database: CheckStatus
    cache: CheckStatus


class ReadyOut(Schema):
    status: Literal["ok", "unavailable"]
    checks: ReadyChecks


def _check_database() -> bool:
    with connections[DEFAULT_DB_ALIAS].cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    return bool(row == (1,))


def _check_cache() -> bool:
    # A unique key per probe, so two probes never read each other's value.
    # The short timeout bounds a leftover key if the delete fails.
    key = f"health:ready:{uuid4().hex}"
    value = uuid4().hex
    cache.set(key, value, timeout=10)
    ok: bool = cache.get(key) == value
    cache.delete(key)
    return ok


def _run(name: str, check: Callable[[], bool]) -> CheckStatus:
    """Run one check. Any failure means "fail", never a 500."""
    try:
        passed = check()
    except Exception as exc:  # Any failure means not ready.
        logger.warning("Readiness check %s failed: %s", name, type(exc).__name__)
        return "fail"
    if not passed:
        logger.warning("Readiness check %s failed: unexpected result", name)
        return "fail"
    return "ok"


@router.get("/live", response=LiveOut, operation_id="health_live")
@decorate_view(never_cache)
def live(request: HttpRequest) -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    response={200: ReadyOut, 503: ReadyOut},
    operation_id="health_ready",
)
@decorate_view(never_cache)
def ready(request: HttpRequest) -> Status[ReadyOut]:
    checks = ReadyChecks(
        database=_run("database", _check_database),
        cache=_run("cache", _check_cache),
    )
    if checks.database == "ok" and checks.cache == "ok":
        return Status(200, ReadyOut(status="ok", checks=checks))
    return Status(503, ReadyOut(status="unavailable", checks=checks))
