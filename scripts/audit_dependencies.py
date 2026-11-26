"""Audit the locked dependencies for known vulnerabilities.

CI runs this on every pull request, every push to main, and weekly
(`.github/workflows/audit.yml`). Run it locally with:

    uv run python scripts/audit_dependencies.py

It checks three things and exits non-zero when any fails:

1. Every accepted exception in `.github/pip-audit-exceptions.toml` has an
   id, a package, a reason, and an expiry date that has not passed.
2. `uv.lock` installs every package from PyPI, except the engine, which
   must be pinned to a version tag and a full commit SHA.
3. pip-audit finds no known vulnerability in the locked dependencies,
   every group included, apart from the accepted exceptions. An exception
   covers its advisory in its own package only.

The engine is left out of pip-audit: it is installed from git, so the
lock file holds its commit but no artifact hash, and no vulnerability
database tracks it. Its pin is checked instead (see docs/engine.md).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EXCEPTIONS_FILE = BASE_DIR / ".github" / "pip-audit-exceptions.toml"
LOCK_FILE = BASE_DIR / "uv.lock"

ENGINE_PACKAGE = "openpickleball-engine"
# A version tag (v1.0.0, v1.1.0rc1) and the full commit it resolved to.
ENGINE_SOURCE = re.compile(
    r"https://github\.com/openpickleball/engine\?rev=v\d+\.\d+\.\d+[a-z0-9.]*"
    r"#[0-9a-f]{40}"
)
PYPI = "https://pypi.org/simple"
# An advisory id has no spaces, such as GHSA-xxxx-xxxx-xxxx or PYSEC-2024-1.
ADVISORY_ID = re.compile(r"[A-Z][A-Z0-9]*-[A-Za-z0-9-]+")


class AuditConfigError(Exception):
    """The exceptions file or the lock file breaks the audit's rules."""


@dataclass(frozen=True)
class AcceptedException:
    """A known vulnerability the project accepts until its expiry date."""

    id: str
    package: str
    reason: str
    expires: dt.date


def normalize(name: str) -> str:
    """Return a package name in its normalized form (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _text(entry: dict[str, object], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AuditConfigError(f"{where}: {key} must be a non-empty string")
    return value.strip()


def load_exceptions(text: str, today: dt.date) -> list[AcceptedException]:
    """Parse the exceptions file, refusing malformed and expired entries."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise AuditConfigError(f"exceptions file is not valid TOML: {error}") from None
    unknown = set(data) - {"exception"}
    if unknown:
        raise AuditConfigError(
            f"exceptions file has unknown keys: {', '.join(sorted(unknown))}"
        )
    entries = data.get("exception", [])
    if not isinstance(entries, list):
        raise AuditConfigError("exception must be an array of tables ([[exception]])")
    accepted: list[AcceptedException] = []
    seen: set[tuple[str, str]] = set()
    for number, entry in enumerate(entries, start=1):
        where = f"exception {number}"
        if not isinstance(entry, dict):
            raise AuditConfigError(f"{where}: must be a table")
        unknown = set(entry) - {"id", "package", "reason", "expires"}
        if unknown:
            raise AuditConfigError(
                f"{where}: unknown keys: {', '.join(sorted(unknown))}"
            )
        advisory = _text(entry, "id", where)
        if not ADVISORY_ID.fullmatch(advisory):
            raise AuditConfigError(f"{where}: id {advisory!r} is not an advisory id")
        where = f"exception {advisory}"
        package = _text(entry, "package", where)
        # One advisory can cover several packages, each with its own entry.
        if (normalize(package), advisory) in seen:
            raise AuditConfigError(f"{where}: listed more than once for {package}")
        seen.add((normalize(package), advisory))
        reason = _text(entry, "reason", where)
        expires = entry.get("expires")
        # A TOML datetime is a datetime, a subclass of date: refuse it too.
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            raise AuditConfigError(
                f"{where}: expires must be a date, such as 2027-01-31"
            )
        if expires < today:
            raise AuditConfigError(
                f"{where}: expired on {expires.isoformat()}; fix the "
                "vulnerability, or review the exception and set a new date"
            )
        accepted.append(AcceptedException(advisory, package, reason, expires))
    return accepted


def check_lock_sources(text: str) -> None:
    """Refuse any package in uv.lock that does not come from PyPI.

    The project itself (a virtual source) and the engine, pinned to a
    version tag and a full commit SHA, are the only exceptions.
    """
    data = tomllib.loads(text)
    engine_found = False
    for package in data.get("package", []):
        name = package.get("name")
        source = package.get("source", {})
        if source == {"virtual": "."}:
            continue
        if name == ENGINE_PACKAGE:
            git = source.get("git")
            if not isinstance(git, str) or not ENGINE_SOURCE.fullmatch(git):
                raise AuditConfigError(
                    f"{ENGINE_PACKAGE} must be installed from a version tag of "
                    "github.com/openpickleball/engine pinned to a full commit SHA"
                )
            engine_found = True
            continue
        if source != {"registry": PYPI}:
            raise AuditConfigError(
                f"{name} is not installed from PyPI, so pip-audit cannot "
                "check it by its hashes"
            )
    if not engine_found:
        raise AuditConfigError(f"{ENGINE_PACKAGE} is missing from uv.lock")


def check_exception_packages(accepted: list[AcceptedException], lock: str) -> None:
    """Refuse an exception for a package the audit does not check.

    That is a package uv.lock no longer has, the engine, or the project.
    """
    locked = {
        normalize(package["name"])
        for package in tomllib.loads(lock)["package"]
        if package["name"] != ENGINE_PACKAGE
        and package.get("source") != {"virtual": "."}
    }
    for exception in accepted:
        if normalize(exception.package) not in locked:
            raise AuditConfigError(
                f"exception {exception.id}: {exception.package} is not a "
                "package the audit checks in uv.lock; remove the exception"
            )


@dataclass(frozen=True)
class Finding:
    """A known vulnerability pip-audit found in a locked package."""

    package: str
    version: str
    id: str
    fix_versions: tuple[str, ...]


def unaccepted_findings(
    report: object, accepted: list[AcceptedException]
) -> list[Finding]:
    """Return the vulnerabilities in a pip-audit JSON report no exception covers.

    An exception covers a vulnerability only in its own package, matched by
    the vulnerability's id or any of its aliases.
    """
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise AuditConfigError("pip-audit's report has no dependencies list")
    covered = {(normalize(item.package), item.id) for item in accepted}
    found = []
    for dependency in report["dependencies"]:
        package = normalize(dependency["name"])
        for vuln in dependency.get("vulns", []):
            ids = {vuln["id"], *vuln.get("aliases", [])}
            if any((package, advisory) in covered for advisory in ids):
                continue
            found.append(
                Finding(
                    dependency["name"],
                    dependency.get("version", ""),
                    vuln["id"],
                    tuple(vuln.get("fix_versions", [])),
                )
            )
    return found


def pip_audit_command(requirements: Path, report: Path) -> list[str]:
    """Build the pip-audit command for an exported requirements file."""
    return [
        sys.executable,
        "-m",
        "pip_audit",
        "--requirement",
        str(requirements),
        # The exported file pins every package with its hashes, so pip-audit
        # reads it as is and installs nothing. With the osv service it checks
        # only that the hashes are present (it warns so); uv checks them
        # against uv.lock when it installs.
        "--require-hashes",
        "--disable-pip",
        "--strict",
        "--vulnerability-service",
        "osv",
        "--progress-spinner",
        "off",
        # Exceptions are applied here, per package, not with --ignore-vuln,
        # which would hide an advisory in every package it covers.
        "--format",
        "json",
        "--output",
        str(report),
    ]


def main() -> int:
    today = dt.datetime.now(dt.UTC).date()
    lock = LOCK_FILE.read_text(encoding="utf-8")
    try:
        accepted = load_exceptions(EXCEPTIONS_FILE.read_text(encoding="utf-8"), today)
        check_lock_sources(lock)
        check_exception_packages(accepted, lock)
    except AuditConfigError as error:
        print(f"audit_dependencies: {error}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as directory:
        requirements = Path(directory) / "requirements.txt"
        report_file = Path(directory) / "report.json"
        exported = subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--all-groups",
                "--no-emit-project",
                "--no-emit-package",
                ENGINE_PACKAGE,
                "--format",
                "requirements-txt",
                "--no-header",
                "--quiet",
                "--output-file",
                str(requirements),
            ],
            check=False,
            cwd=BASE_DIR,
        )
        if exported.returncode != 0:
            # uv has printed why, such as a lock file out of step with
            # pyproject.toml.
            print("audit_dependencies: uv export failed", file=sys.stderr)
            return exported.returncode
        audited = subprocess.run(
            pip_audit_command(requirements, report_file), check=False
        )
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
            found = unaccepted_findings(report, accepted)
            total = sum(len(item.get("vulns", [])) for item in report["dependencies"])
        except (OSError, ValueError, KeyError, TypeError, AuditConfigError):
            print("audit_dependencies: pip-audit gave no report", file=sys.stderr)
            return audited.returncode or 1
    # pip-audit exits 1 when it finds any vulnerability, accepted ones
    # included. Any other non-zero exit is an error, such as --strict
    # refusing a package it could not audit.
    if audited.returncode not in (0, 1):
        print("audit_dependencies: pip-audit failed", file=sys.stderr)
        return audited.returncode
    for exception in accepted:
        print(
            f"Accepted until {exception.expires.isoformat()}: "
            f"{exception.id} in {exception.package}"
        )
    for finding in found:
        fixes = ", ".join(finding.fix_versions) or "no fixed version"
        print(f"{finding.package} {finding.version}: {finding.id} (fixed in {fixes})")
    if found:
        print(
            f"audit_dependencies: {len(found)} known vulnerabilities", file=sys.stderr
        )
        return 1
    if audited.returncode != 0 and total == 0:
        # Exit 1 with nothing found is an error pip-audit reported.
        print("audit_dependencies: pip-audit failed", file=sys.stderr)
        return audited.returncode
    print("No known vulnerabilities found, apart from accepted exceptions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
