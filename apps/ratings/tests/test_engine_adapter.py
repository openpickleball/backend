"""The engine is installed from a git tag, and the backend reports its version.

The tag in ``pyproject.toml``, the tag in ``uv.lock``, and the version of the
installed package must agree.
"""

import re
import tomllib
from importlib.metadata import version
from typing import Any

import open_engine
from django.conf import settings

from apps.ratings.engine_adapter import engine_version

ENGINE_DISTRIBUTION = "openpickleball-engine"
ENGINE_REPOSITORY = "https://github.com/openpickleball/engine"


def _read_toml(name: str) -> dict[str, Any]:
    with (settings.BASE_DIR / name).open("rb") as file:
        return tomllib.load(file)


def test_engine_version_is_the_installed_engine() -> None:
    assert engine_version() == open_engine.__version__
    assert engine_version() == version(ENGINE_DISTRIBUTION)


def test_lock_pins_the_engine_to_a_tag_and_commit() -> None:
    packages = _read_toml("uv.lock")["package"]
    [engine] = [p for p in packages if p["name"] == ENGINE_DISTRIBUTION]

    match = re.fullmatch(
        re.escape(ENGINE_REPOSITORY) + r"\?rev=(v[^#]+)#([0-9a-f]{40})",
        engine["source"]["git"],
    )

    assert match is not None
    assert match.group(1) == f"v{engine_version()}"
    assert engine["version"] == engine_version()


def test_pyproject_pins_the_engine_to_the_locked_tag() -> None:
    dependencies = _read_toml("pyproject.toml")["project"]["dependencies"]
    entries = [d for d in dependencies if d.startswith(ENGINE_DISTRIBUTION)]

    assert entries == [
        f"{ENGINE_DISTRIBUTION} @ git+{ENGINE_REPOSITORY}@v{engine_version()}"
    ]
