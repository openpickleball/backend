"""`export_openapi_schema --check` compares the file's bytes (#42 review)."""

from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from apps.openapi.schema import render_schema


def test_check_fails_on_crlf_line_endings(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    output.write_text(render_schema(), encoding="utf-8", newline="\r\n")

    with pytest.raises(CommandError, match="out of date"):
        call_command("export_openapi_schema", "--output", str(output), "--check")


def test_check_fails_on_invalid_utf8(tmp_path: Path) -> None:
    output = tmp_path / "openapi.json"
    output.write_bytes(render_schema().encode("utf-8") + b"\xff\xfe")

    with pytest.raises(CommandError, match="out of date"):
        call_command("export_openapi_schema", "--output", str(output), "--check")
