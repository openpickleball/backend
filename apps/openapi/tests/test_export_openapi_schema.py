"""The committed OpenAPI document and the command that writes it (#42)."""

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError, call_command, get_commands
from django.test import Client
from pytest_django.fixtures import Settings

from apps.openapi.schema import render_schema, schema_path
from config.api import api

COMMAND = "export_openapi_schema"


def _export(*args: str) -> str:
    out = StringIO()
    call_command(COMMAND, *args, stdout=out)
    return out.getvalue()


def test_command_is_opens() -> None:
    assert get_commands()[COMMAND] == "apps.openapi"


def test_writes_schema(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    message = _export("--output", str(output))

    text = output.read_text(encoding="utf-8")
    assert text == render_schema()
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    document = json.loads(text)
    assert document["openapi"].startswith("3.")
    assert any(path.startswith("/api/v1/") for path in document["paths"])
    assert str(output) in message


def test_output_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    _export("--output", str(first))
    _export("--output", str(second))

    assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in first.read_bytes()


def test_default_output_is_repository_file(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.BASE_DIR = tmp_path / "repo"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    _export()

    written = tmp_path / "repo" / "openapi" / "openapi.json"
    assert schema_path() == written
    assert written.read_text(encoding="utf-8") == render_schema()
    assert not (elsewhere / "openapi").exists()


def test_check_passes_when_current(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    output.write_text(render_schema(), encoding="utf-8", newline="\n")

    _export("--output", str(output), "--check")


def test_check_fails_on_drift(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    stale = render_schema().replace("OPEN API", "OLD API")
    output.write_text(stale, encoding="utf-8", newline="\n")

    with pytest.raises(CommandError) as raised:
        _export("--output", str(output), "--check")

    message = str(raised.value)
    assert "out of date" in message
    assert "uv run python manage.py export_openapi_schema" in message
    assert '"paths"' not in message
    assert output.read_text(encoding="utf-8") == stale


def test_check_fails_when_missing(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"

    with pytest.raises(CommandError) as raised:
        _export("--output", str(output), "--check")

    assert "missing" in str(raised.value)
    assert "uv run python manage.py export_openapi_schema" in str(raised.value)
    assert not output.exists()


def test_check_fails_when_an_endpoint_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "openapi.json"
    _export("--output", str(output))
    original = api.get_openapi_schema

    def changed(**kwargs: Any) -> Any:
        schema = original(**kwargs)
        schema["paths"]["/api/v1/new"] = {}
        return schema

    monkeypatch.setattr(api, "get_openapi_schema", changed)

    with pytest.raises(CommandError, match="out of date"):
        _export("--output", str(output), "--check")


def test_committed_schema_is_current() -> None:
    _export("--check")


def test_served_document_matches_committed_file(client: Client) -> None:
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    assert response.json() == json.loads(schema_path().read_text(encoding="utf-8"))
