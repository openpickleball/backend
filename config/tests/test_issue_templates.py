"""The issue forms in .github/ISSUE_TEMPLATE stay complete and consistent."""

from pathlib import Path
from typing import Any

import pytest
import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
GITHUB_DIR = BASE_DIR / ".github"
TEMPLATE_DIR = GITHUB_DIR / "ISSUE_TEMPLATE"
ORG = "https://github.com/openpickleball/.github"
CHARTER_URL = f"{ORG}/blob/main/CHARTER.md"
CODE_OF_CONDUCT_URL = f"{ORG}/blob/main/CODE_OF_CONDUCT.md"
NO_PLAYER_DATA = "real player names, contact details, or match histories"

FORMS: dict[str, tuple[list[str], list[str]]] = {
    "task.yml": (
        ["triage"],
        ["context", "tasks", "acceptance-criteria", "depends-on", "size"],
    ),
    "bug_report.yml": (
        ["bug", "triage"],
        [
            "what-happened",
            "expected",
            "steps",
            "version",
            "environment",
            "acceptance-criteria",
        ],
    ),
    "epic.yml": (
        ["type:epic"],
        ["problem", "scope", "acceptance-criteria", "out-of-scope"],
    ),
}


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((TEMPLATE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _elements(name: str) -> list[dict[str, Any]]:
    body = _load(name)["body"]
    assert isinstance(body, list)
    return body


def _element(name: str, element_id: str) -> dict[str, Any]:
    matches = [e for e in _elements(name) if e.get("id") == element_id]
    assert len(matches) == 1
    return matches[0]


def test_template_dir_holds_exactly_the_three_forms_and_config() -> None:
    names = {path.name for path in TEMPLATE_DIR.iterdir()}
    assert names == {*FORMS, "config.yml"}


@pytest.mark.parametrize("name", FORMS)
def test_form_has_name_description_and_labels(name: str) -> None:
    form = _load(name)
    assert isinstance(form["name"], str)
    assert form["name"]
    assert isinstance(form["description"], str)
    assert form["description"]
    assert form["labels"] == FORMS[name][0]


@pytest.mark.parametrize("name", FORMS)
def test_form_element_ids_are_unique(name: str) -> None:
    ids = [e["id"] for e in _elements(name) if "id" in e]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", FORMS)
def test_form_fields_match_expected_ids(name: str) -> None:
    ids = [e["id"] for e in _elements(name) if "id" in e and e["type"] != "checkboxes"]
    assert ids == FORMS[name][1]


@pytest.mark.parametrize("name", FORMS)
def test_form_has_acceptance_criteria_textarea(name: str) -> None:
    assert _element(name, "acceptance-criteria")["type"] == "textarea"


@pytest.mark.parametrize("name", FORMS)
def test_form_ends_with_required_agreements(name: str) -> None:
    last = _elements(name)[-1]
    assert last["type"] == "checkboxes"
    assert last["id"] == "agreements"
    options = last["attributes"]["options"]
    labels = [option["label"] for option in options]
    assert len(labels) == 3
    assert CHARTER_URL in labels[0]
    assert NO_PLAYER_DATA in labels[1]
    assert CODE_OF_CONDUCT_URL in labels[2]
    assert all(option["required"] is True for option in options)


def test_task_size_offers_small_medium_large() -> None:
    size = _element("task.yml", "size")
    assert size["type"] == "dropdown"
    assert size["attributes"]["options"] == ["S", "M", "L"]


@pytest.mark.parametrize(
    ("name", "required_ids"),
    [
        ("task.yml", {"context", "tasks", "acceptance-criteria"}),
        ("bug_report.yml", {"what-happened", "expected", "steps"}),
        ("epic.yml", {"problem", "scope", "acceptance-criteria"}),
    ],
)
def test_form_requires_its_core_fields(name: str, required_ids: set[str]) -> None:
    required = {
        e["id"]
        for e in _elements(name)
        if e["type"] != "checkboxes"
        and e.get("validations", {}).get("required") is True
    }
    assert required == required_ids


def test_bug_form_keeps_the_security_notice() -> None:
    first = _elements("bug_report.yml")[0]
    assert first["type"] == "markdown"
    assert f"{ORG}/blob/main/SECURITY.md" in first["attributes"]["value"]


def test_config_disables_blank_issues_and_links_org_forms() -> None:
    config = _load("config.yml")
    assert config["blank_issues_enabled"] is False
    urls = [link["url"] for link in config["contact_links"]]
    assert urls == [
        f"{ORG}/issues/new?template=community_feedback.yml",
        f"{ORG}/issues/new?template=rating_spec_change.yml",
        f"{ORG}/blob/main/SECURITY.md",
        f"{ORG}/discussions",
    ]


def test_no_pull_request_template() -> None:
    names = {path.name.lower() for path in GITHUB_DIR.iterdir()}
    assert "pull_request_template.md" not in names
    assert "pull_request_template" not in names
