"""compose.yaml keeps the app containers out of pooled mode (ADR 0038).

The web, worker, and beat services mount the repository, so settings read
its .env. A developer who copied the pooled lines from .env.example has
DIRECT_DATABASE_URL pointing at localhost, which inside a container is the
container itself: migrate would fail and beat would wait forever. Each
service that mounts the repository pins it to empty, like DATABASE_URL.
"""

from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _services_reading_env() -> dict[str, dict[str, Any]]:
    compose = yaml.safe_load((BASE_DIR / "compose.yaml").read_text())
    services: dict[str, dict[str, Any]] = compose["services"]
    return {
        name: service
        for name, service in services.items()
        if any(str(volume).startswith(".:") for volume in service.get("volumes", []))
    }


def test_services_that_mount_the_repository_are_found() -> None:
    assert set(_services_reading_env()) == {"web", "worker", "beat"}


def test_services_that_read_env_pin_the_direct_url_empty() -> None:
    for name, service in _services_reading_env().items():
        environment = service.get("environment", {})
        assert environment.get("DIRECT_DATABASE_URL") == "", name
