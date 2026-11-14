"""CODEOWNERS and .editorconfig stay valid and agree with ruff format."""

import configparser
import subprocess
from pathlib import Path

import pytest
from ruff import find_ruff_bin  # type: ignore[import-untyped]

BASE_DIR = Path(__file__).resolve().parent.parent.parent
CODEOWNERS = BASE_DIR / ".github" / "CODEOWNERS"
EDITORCONFIG = BASE_DIR / ".editorconfig"
MAINTAINERS = "@openpickleball/maintainers"


def _codeowners_rules() -> list[tuple[str, list[str]]]:
    rules = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pattern, *owners = stripped.split()
        rules.append((pattern, owners))
    return rules


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
        cwd=BASE_DIR,
    )
    return [path for path in result.stdout.split("\0") if path]


def _codeowners_pattern_owns_tracked(pattern: str, tracked: list[str]) -> bool:
    """Return whether an anchored CODEOWNERS pattern owns a tracked path.

    A pattern ending in `/` matches only a directory, so it must be a
    directory prefix of a tracked path. Any other pattern must be a tracked
    file or a directory prefix of a tracked path.
    """
    path = pattern.strip("/")
    if not path:
        return False
    is_directory = any(name.startswith(f"{path}/") for name in tracked)
    if pattern.endswith("/"):
        return is_directory
    return is_directory or path in tracked


def _editorconfig() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    text = EDITORCONFIG.read_text(encoding="utf-8")
    # editorconfig allows preamble keys (root = true) before the first section.
    parser.read_string(f"[__preamble__]\n{text}")
    return parser


def _ruff_formatter_settings() -> dict[str, str]:
    result = subprocess.run(
        [find_ruff_bin(), "check", "--show-settings", str(BASE_DIR / "manage.py")],
        capture_output=True,
        text=True,
        check=True,
        cwd=BASE_DIR,
    )
    settings: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition(" = ")
        if sep and key.startswith("formatter."):
            settings[key.strip()] = value.strip()
    return settings


def test_codeowners_default_owner_is_maintainers() -> None:
    rules = _codeowners_rules()

    assert rules[0] == ("*", [MAINTAINERS])


def test_codeowners_paths_exist() -> None:
    patterns = [pattern for pattern, _ in _codeowners_rules() if pattern != "*"]

    tracked = _tracked_files()

    missing = [p for p in patterns if not _codeowners_pattern_owns_tracked(p, tracked)]

    assert patterns
    assert missing == []


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("/apps/accounts/", True),
        ("/.github/", True),
        ("/manage.py", True),
        ("/apps", True),
        ("/.env", False),
        ("/manage.py/", False),
        ("/no-such-dir/", False),
        ("/no-such-file", False),
        ("/apps/acc", False),
    ],
)
def test_codeowners_pattern_owns_tracked(pattern: str, *, expected: bool) -> None:
    tracked = _tracked_files()

    assert _codeowners_pattern_owns_tracked(pattern, tracked) is expected


def test_codeowners_owners_are_maintainers() -> None:
    for pattern, owners in _codeowners_rules():
        assert owners == [MAINTAINERS], pattern


def test_codeowners_covers_accounts_and_github() -> None:
    patterns = {pattern for pattern, _ in _codeowners_rules()}

    assert "/apps/accounts/" in patterns
    assert "/.github/" in patterns


def test_editorconfig_matches_ruff_format() -> None:
    python = _editorconfig()["*.{py,pyi}"]
    ruff = _ruff_formatter_settings()

    assert ruff["formatter.indent_width"] == python["indent_size"]
    assert ruff["formatter.line_width"] == python["max_line_length"]
    assert ruff["formatter.indent_style"] == "space"
    assert ruff["formatter.line_ending"] in {"auto", "lf"}


def test_editorconfig_base_settings() -> None:
    config = _editorconfig()

    assert config["__preamble__"]["root"] == "true"
    assert dict(config["*"]) == {
        "charset": "utf-8",
        "end_of_line": "lf",
        "insert_final_newline": "true",
        "trim_trailing_whitespace": "true",
        "indent_style": "space",
    }
    assert config["*.{yml,yaml}"]["indent_size"] == "2"
    assert config["*.toml"]["indent_size"] == "4"
