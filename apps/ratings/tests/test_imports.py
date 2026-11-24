"""Only the engine adapter imports the rating engine (#44).

Every other module reaches ``open_engine`` through
``apps.ratings.engine_adapter``, so the mapping rules live in one place.
Tests may import the engine to check the adapter's output.
"""

import ast
from pathlib import Path

from django.conf import settings

ADAPTER = Path("apps") / "ratings" / "engine_adapter.py"
ENGINE = "open_engine"


def _is_test_module(relative: Path) -> bool:
    return (
        "tests" in relative.parts
        or relative.name.startswith("test_")
        or relative.name == "conftest.py"
    )


def _imports_engine(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        else:
            continue
        if any(name == ENGINE or name.startswith(f"{ENGINE}.") for name in names):
            return True
    return False


def _engine_importers() -> set[Path]:
    base = Path(settings.BASE_DIR)
    found: set[Path] = set()
    for top in ("apps", "config"):
        for path in (base / top).rglob("*.py"):
            relative = path.relative_to(base)
            if _is_test_module(relative):
                continue
            if _imports_engine(path.read_text(encoding="utf-8")):
                found.add(relative)
    return found


def test_only_the_adapter_imports_the_engine() -> None:
    assert _engine_importers() == {ADAPTER}


def test_the_import_check_sees_each_form() -> None:
    assert _imports_engine("import open_engine")
    assert _imports_engine("import open_engine.engine as e")
    assert _imports_engine("from open_engine import Match")
    assert _imports_engine("from open_engine._model import Match")
    assert _imports_engine("def f():\n    import open_engine\n")
    assert not _imports_engine('"""Calls open_engine through the adapter."""')
    assert not _imports_engine("import open_engine_extra")
    assert not _imports_engine("from . import open_engine")
