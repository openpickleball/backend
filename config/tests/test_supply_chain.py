"""Supply chain checks stay in place (issue #104).

Workflows pin every action to a commit SHA and set their permissions,
every install is locked, Dependabot covers each ecosystem, and the
dependency audit refuses expired exceptions and unpinned sources.
"""

import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import audit_dependencies as audit

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GITHUB_DIR = BASE_DIR / ".github"
WORKFLOWS = sorted((GITHUB_DIR / "workflows").glob("*.yml"))
TODAY = dt.date(2026, 11, 25)
ENGINE_GIT = (
    "https://github.com/openpickleball/engine?rev=v1.0.0"
    "#be2a0ff59065f7273b5dc13e395e5ec25ee01d3c"
)
PINNED_ACTION = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _uses(workflow: dict[str, Any]) -> list[str]:
    found = []
    for job in workflow["jobs"].values():
        if "uses" in job:
            found.append(job["uses"])
        found.extend(step["uses"] for step in job.get("steps", []) if "uses" in step)
    return found


def test_there_are_workflows() -> None:
    names = {path.name for path in WORKFLOWS}
    assert {"ci.yml", "audit.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_actions_are_pinned_to_commit_shas(path: Path) -> None:
    for action in _uses(_load(path)):
        assert PINNED_ACTION.fullmatch(action), action
    # Each pin carries the version it stands for, which Dependabot updates.
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"\s*(- )?uses:", line):
            assert re.search(r"@[0-9a-f]{40} # v\d", line), line


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_workflows_set_top_level_permissions(path: Path) -> None:
    permissions = _load(path).get("permissions")
    # Write access, where a job needs it, is granted on that job alone.
    assert permissions == "read-all" or (
        isinstance(permissions, dict)
        and permissions
        and all(value == "read" for value in permissions.values())
    )
    for job in _load(path)["jobs"].values():
        assert job.get("permissions") != "write-all"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_every_uv_sync_is_locked(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if "uv sync" in line:
            assert "--locked" in line, line


def test_image_installs_from_the_lock() -> None:
    lines = [
        line
        for line in (BASE_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if "uv sync" in line
    ]
    assert lines
    assert all("--locked" in line for line in lines)


def test_audit_workflow_runs_on_pull_requests_main_and_weekly() -> None:
    workflow = _load(GITHUB_DIR / "workflows" / "audit.yml")
    # PyYAML reads the bare key `on` as True.
    triggers = workflow[True]
    assert "pull_request" in triggers
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["schedule"]
    steps = [step.get("run") for step in workflow["jobs"]["audit"]["steps"]]
    assert "uv sync --locked" in steps
    assert "uv run python scripts/audit_dependencies.py" in steps


def test_dependabot_covers_each_ecosystem_weekly_and_grouped() -> None:
    config = _load(GITHUB_DIR / "dependabot.yml")
    updates = {update["package-ecosystem"]: update for update in config["updates"]}
    assert set(updates) == {"uv", "github-actions", "docker"}
    for update in updates.values():
        assert update["directory"] == "/"
        assert update["schedule"]["interval"] == "weekly"
        assert update["groups"]
    for ecosystem in ("uv", "github-actions"):
        (group,) = updates[ecosystem]["groups"].values()
        assert group["update-types"] == ["minor", "patch"]
    # The engine is upgraded by hand (docs/engine.md).
    assert updates["uv"]["ignore"] == [{"dependency-name": audit.ENGINE_PACKAGE}]
    # The Python minor stays the one .python-version names.
    (ignore,) = updates["docker"]["ignore"]
    assert ignore["dependency-name"] == "python"
    assert set(ignore["update-types"]) == {
        "version-update:semver-major",
        "version-update:semver-minor",
    }


def test_committed_files_pass_the_audit_checks() -> None:
    today = dt.datetime.now(dt.UTC).date()
    lock = audit.LOCK_FILE.read_text(encoding="utf-8")
    accepted = audit.load_exceptions(
        audit.EXCEPTIONS_FILE.read_text(encoding="utf-8"), today
    )
    audit.check_lock_sources(lock)
    audit.check_exception_packages(accepted, lock)


def _exception(**overrides: str) -> str:
    entry = {
        "id": '"GHSA-abcd-efgh-ijkl"',
        "package": '"example"',
        "reason": '"Not reachable from OPEN."',
        "expires": "2026-12-31",
    } | overrides
    lines = [f"{key} = {value}" for key, value in entry.items() if value]
    return "[[exception]]\n" + "\n".join(lines) + "\n"


def test_exception_is_loaded() -> None:
    (accepted,) = audit.load_exceptions(_exception(), TODAY)
    assert accepted == audit.AcceptedException(
        "GHSA-abcd-efgh-ijkl",
        "example",
        "Not reachable from OPEN.",
        dt.date(2026, 12, 31),
    )


def test_no_exceptions_is_fine() -> None:
    assert audit.load_exceptions("# none\n", TODAY) == []


def test_exception_expiring_today_still_applies() -> None:
    assert audit.load_exceptions(_exception(expires="2026-11-25"), TODAY)


def test_expired_exception_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="expired on 2026-11-24"):
        audit.load_exceptions(_exception(expires="2026-11-24"), TODAY)


@pytest.mark.parametrize("key", ["id", "package", "reason", "expires"])
def test_exception_missing_a_key_fails(key: str) -> None:
    with pytest.raises(audit.AuditConfigError):
        audit.load_exceptions(_exception(**{key: ""}), TODAY)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reason", '"   "'),
        ("expires", '"2026-12-31"'),
        ("expires", "2026-12-31T00:00:00"),
        ("id", '"not an id"'),
        ("id", "7"),
    ],
)
def test_malformed_exception_fails(key: str, value: str) -> None:
    with pytest.raises(audit.AuditConfigError):
        audit.load_exceptions(_exception(**{key: value}), TODAY)


def test_unknown_exception_key_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="unknown keys: note"):
        audit.load_exceptions(_exception() + 'note = "x"\n', TODAY)


def test_unknown_top_level_key_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="unknown keys: exceptions"):
        audit.load_exceptions("[[exceptions]]\n", TODAY)


def test_duplicate_exception_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="more than once"):
        audit.load_exceptions(_exception() + _exception(), TODAY)


def test_invalid_toml_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="not valid TOML"):
        audit.load_exceptions("[[exception]\n", TODAY)


def _lock(*packages: tuple[str, str]) -> str:
    body = [
        'version = 1\n\n[[package]]\nname = "open-backend"\nsource = { virtual = "." }\n'
    ]
    body += [
        f'[[package]]\nname = "{name}"\nsource = {source}\n'
        for name, source in packages
    ]
    return "\n".join(body)


PYPI_SOURCE = '{ registry = "https://pypi.org/simple" }'


def test_lock_with_pypi_and_pinned_engine_passes() -> None:
    audit.check_lock_sources(
        _lock(
            ("django", PYPI_SOURCE),
            (audit.ENGINE_PACKAGE, f'{{ git = "{ENGINE_GIT}" }}'),
        )
    )


@pytest.mark.parametrize(
    "git",
    [
        # A branch, not a version tag.
        "https://github.com/openpickleball/engine?rev=main#be2a0ff59065f7273b5dc13e395e5ec25ee01d3c",
        # No commit.
        "https://github.com/openpickleball/engine?rev=v1.0.0",
        # A short commit.
        "https://github.com/openpickleball/engine?rev=v1.0.0#be2a0ff",
        # Another repository.
        "https://github.com/someone/engine?rev=v1.0.0#be2a0ff59065f7273b5dc13e395e5ec25ee01d3c",
    ],
)
def test_engine_not_pinned_to_a_tag_and_commit_fails(git: str) -> None:
    with pytest.raises(audit.AuditConfigError, match="version tag"):
        audit.check_lock_sources(_lock((audit.ENGINE_PACKAGE, f'{{ git = "{git}" }}')))


def test_engine_from_pypi_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="version tag"):
        audit.check_lock_sources(_lock((audit.ENGINE_PACKAGE, PYPI_SOURCE)))


def test_missing_engine_fails() -> None:
    with pytest.raises(audit.AuditConfigError, match="missing"):
        audit.check_lock_sources(_lock(("django", PYPI_SOURCE)))


@pytest.mark.parametrize(
    "source",
    [
        '{ git = "https://github.com/example/package?rev=v1#be2a0ff59065f7273b5dc13e395e5ec25ee01d3c" }',
        '{ registry = "https://example.org/simple" }',
        '{ url = "https://example.org/package.whl" }',
        '{ path = "vendor/package" }',
    ],
)
def test_other_package_not_from_pypi_fails(source: str) -> None:
    lock = _lock(
        ("other", source), (audit.ENGINE_PACKAGE, f'{{ git = "{ENGINE_GIT}" }}')
    )
    with pytest.raises(
        audit.AuditConfigError, match="other is not installed from PyPI"
    ):
        audit.check_lock_sources(lock)


def test_pip_audit_command_writes_a_json_report() -> None:
    command = audit.pip_audit_command(Path("requirements.txt"), Path("report.json"))
    assert command[1:3] == ["-m", "pip_audit"]
    assert "--strict" in command
    assert "--require-hashes" in command
    assert command[-4:] == ["--format", "json", "--output", "report.json"]
    # Exceptions are applied per package by the script, never by id alone.
    assert "--ignore-vuln" not in command


def _report(*vulns: tuple[str, str, list[str]]) -> dict[str, Any]:
    return {
        "dependencies": [
            {"name": "Jinja2", "version": "2.11.2", "vulns": []},
            *(
                {
                    "name": name,
                    "version": "1.0",
                    "vulns": [
                        {"id": advisory, "fix_versions": ["1.1"], "aliases": aliases}
                    ],
                }
                for name, advisory, aliases in vulns
            ),
        ]
    }


def test_findings_without_exceptions_are_reported() -> None:
    (finding,) = audit.unaccepted_findings(_report(("Example", "PYSEC-2026-1", [])), [])
    assert finding == audit.Finding("Example", "1.0", "PYSEC-2026-1", ("1.1",))


def test_exception_covers_its_package_by_id_or_alias() -> None:
    accepted = audit.load_exceptions(_exception(), TODAY)
    by_id = _report(("example", "GHSA-abcd-efgh-ijkl", []))
    by_alias = _report(("Example", "PYSEC-2026-1", ["GHSA-abcd-efgh-ijkl"]))
    assert audit.unaccepted_findings(by_id, accepted) == []
    assert audit.unaccepted_findings(by_alias, accepted) == []


def test_exception_does_not_cover_another_package() -> None:
    accepted = audit.load_exceptions(_exception(), TODAY)
    report = _report(("other", "GHSA-abcd-efgh-ijkl", []))
    (finding,) = audit.unaccepted_findings(report, accepted)
    assert finding.package == "other"


@pytest.mark.parametrize("report", [None, [], {}, {"dependencies": "x"}])
def test_malformed_report_fails(report: object) -> None:
    with pytest.raises(audit.AuditConfigError):
        audit.unaccepted_findings(report, [])


def test_exception_for_a_package_not_in_the_lock_fails() -> None:
    lock = _lock(("Example_Package", PYPI_SOURCE))
    accepted = audit.load_exceptions(_exception(package='"example.package"'), TODAY)
    audit.check_exception_packages(accepted, lock)
    stale = audit.load_exceptions(_exception(package='"gone"'), TODAY)
    with pytest.raises(
        audit.AuditConfigError, match="gone is not a package the audit checks"
    ):
        audit.check_exception_packages(stale, lock)


def test_exception_for_the_engine_or_the_project_fails() -> None:
    lock = _lock((audit.ENGINE_PACKAGE, f'{{ git = "{ENGINE_GIT}" }}'))
    for package in (audit.ENGINE_PACKAGE, "open-backend"):
        accepted = audit.load_exceptions(_exception(package=f'"{package}"'), TODAY)
        with pytest.raises(audit.AuditConfigError, match="not a package"):
            audit.check_exception_packages(accepted, lock)


def test_one_advisory_may_cover_two_packages() -> None:
    text = _exception() + _exception(package='"other"')
    assert len(audit.load_exceptions(text, TODAY)) == 2


class _Run:
    """Stands in for subprocess.run: uv export, then pip-audit."""

    def __init__(
        self, audit_exit: int, report: object | None, uv_exit: int = 0
    ) -> None:
        self.audit_exit = audit_exit
        self.report = report
        self.uv_exit = uv_exit

    def __call__(self, command: list[str], **kwargs: object) -> Any:
        if command[0] == "uv":
            return subprocess.CompletedProcess(command, self.uv_exit)
        if self.report is not None:
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps(self.report), encoding="utf-8")
        return subprocess.CompletedProcess(command, self.audit_exit)


@pytest.fixture
def audit_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the audit at a made-up lock file; return its exceptions file."""
    lock = tmp_path / "uv.lock"
    lock.write_text(
        _lock(
            ("django", PYPI_SOURCE),
            ("celery", PYPI_SOURCE),
            (audit.ENGINE_PACKAGE, f'{{ git = "{ENGINE_GIT}" }}'),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "LOCK_FILE", lock)
    exceptions = tmp_path / "exceptions.toml"
    monkeypatch.setattr(audit, "EXCEPTIONS_FILE", exceptions)
    return exceptions


@pytest.mark.parametrize(
    ("audit_exit", "report", "expected"),
    [
        (0, {"dependencies": []}, 0),
        # pip-audit exits 1 for accepted findings too.
        (1, "accepted", 0),
        (1, "unaccepted", 1),
        # Exit 1 with nothing found is an error.
        (1, {"dependencies": []}, 1),
        (1, None, 1),
        (2, None, 2),
        (2, "accepted", 2),
        (0, {}, 1),
    ],
)
def test_main_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    audit_files: Path,
    audit_exit: int,
    report: object,
    expected: int,
) -> None:
    audit_files.write_text(
        _exception(package='"django"', expires="2999-01-01"), encoding="utf-8"
    )
    finding = {
        "name": "Django",
        "version": "1.0",
        "vulns": [{"id": "GHSA-abcd-efgh-ijkl", "fix_versions": [], "aliases": []}],
    }
    if report == "accepted":
        report = {"dependencies": [finding]}
    elif report == "unaccepted":
        report = {"dependencies": [finding | {"name": "celery"}]}
    monkeypatch.setattr(subprocess, "run", _Run(audit_exit, report))
    assert audit.main() == expected


def test_main_refuses_an_expired_exception_before_auditing(
    monkeypatch: pytest.MonkeyPatch, audit_files: Path
) -> None:
    audit_files.write_text(
        _exception(package='"django"', expires="2000-01-01"), encoding="utf-8"
    )
    monkeypatch.setattr(subprocess, "run", _Run(0, {"dependencies": []}))
    assert audit.main() == 1


def test_main_stops_when_uv_export_fails(
    monkeypatch: pytest.MonkeyPatch, audit_files: Path
) -> None:
    audit_files.write_text("", encoding="utf-8")
    monkeypatch.setattr(subprocess, "run", _Run(0, {"dependencies": []}, uv_exit=2))
    assert audit.main() == 2
