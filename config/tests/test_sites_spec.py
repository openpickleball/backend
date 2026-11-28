"""Tests for the App Platform app spec .do/sites.yaml (issue #304).

The sites app serves OPEN's static sites that need no API: the public
site on openpb.org and the developer site on dev.openpb.org. It holds
static sites only, so it stays on App Platform's free tier.
"""

import re
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SPEC_PATH = BASE_DIR / ".do" / "sites.yaml"

APEX = "openpb.org"
WWW = "www.openpb.org"
DEV = "dev.openpb.org"

SITE = {
    "name": "site",
    "git": {
        "repo_clone_url": "https://github.com/openpickleball/frontend.git",
        "branch": "main",
    },
    "source_dir": "site",
    "environment_slug": "node-js",
    "build_command": (
        "pnpm build && mkdir -p public/.well-known && "
        "cp ../.well-known/funding-manifest-urls public/.well-known/"
    ),
    "output_dir": "public",
    "error_document": "404.html",
}
DEVELOPERS = {
    "name": "developers",
    "git": {
        "repo_clone_url": "https://github.com/openpickleball/developers.git",
        "branch": "main",
    },
    "environment_slug": "python",
    "build_command": "zensical build --strict",
    "output_dir": "site",
    "error_document": "404.html",
}


def _spec() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text())
    return spec


def _strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [s for value in node.values() for s in _strings(value)]
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return [node] if isinstance(node, str) else []


def _rules() -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = _spec()["ingress"]["rules"]
    return rules


def test_the_sites_app_holds_only_static_sites() -> None:
    spec = _spec()

    assert spec["name"] == "open-sites"
    # Any other component would take the app off the free tier.
    assert set(spec) == {"name", "static_sites", "domains", "ingress"}
    assert [s["name"] for s in spec["static_sites"]] == ["site", "developers"]


def test_the_sites_app_has_no_secrets_bindings_or_region() -> None:
    spec = _spec()

    assert "envs" not in spec
    assert "region" not in spec
    for site in spec["static_sites"]:
        assert "envs" not in site, site["name"]
    assert not any("${" in value for value in _strings(spec))
    assert "SECRET" not in _strings(spec)


def test_the_public_site_builds_from_the_frontend() -> None:
    # The well-known file sits at the repository root, outside site/,
    # so the build copies it into the output.
    assert _spec()["static_sites"][0] == SITE


def test_the_developer_site_builds_with_zensical() -> None:
    assert _spec()["static_sites"][1] == DEVELOPERS


def test_the_partner_component_waits_for_its_repository() -> None:
    # partner-docs does not exist yet (#184); the spec only notes it.
    names = [s["name"] for s in _spec()["static_sites"]]
    text = SPEC_PATH.read_text()

    assert "partner" not in names
    # A comment names the planned component and the issue it waits for.
    assert re.search(
        r"^\s*# The partner docs \(openpickleball/partner-docs, partner\.openpb\.org\)$",
        text,
        re.MULTILINE,
    )
    assert re.search(r"^\s*#.*#184", text, re.MULTILINE)


def test_the_sites_app_answers_on_the_three_hosts() -> None:
    domains = _spec()["domains"]

    assert [(d["domain"], d["type"]) for d in domains] == [
        (APEX, "PRIMARY"),
        (WWW, "ALIAS"),
        (DEV, "ALIAS"),
    ]
    for domain in domains:
        assert domain["minimum_tls_version"] == "1.2", domain["domain"]
        assert not domain.get("wildcard", False), domain["domain"]
        # DNS for openpb.org is not on DigitalOcean.
        assert "zone" not in domain, domain["domain"]


def test_every_rule_names_its_host() -> None:
    for rule in _rules():
        assert set(rule["match"]) == {"authority", "path"}, rule
        assert set(rule["match"]["authority"]) == {"exact"}, rule
        assert rule["match"]["path"] == {"prefix": "/"}, rule


def test_each_host_goes_to_its_site_and_www_redirects() -> None:
    by_host = {r["match"]["authority"]["exact"]: r for r in _rules()}

    assert len(by_host) == len(_rules())
    assert set(by_host) == {d["domain"] for d in _spec()["domains"]}
    # Exact keys, so an added redirect or cors on any rule fails.
    assert set(by_host[APEX]) == {"match", "component"}
    assert set(by_host[DEV]) == {"match", "component"}
    assert set(by_host[WWW]) == {"match", "redirect"}
    assert by_host[APEX]["component"] == {"name": "site"}
    assert by_host[DEV]["component"] == {"name": "developers"}
    assert by_host[WWW]["redirect"] == {
        "authority": APEX,
        "scheme": "https",
        "redirect_code": 301,
    }
