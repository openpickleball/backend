"""The pull request checks stay wired to the organization's shared check.

The rules and their tests live in openpickleball/.github
(`scripts/check_pull_request.py`). This repository calls its reusable
`pull-request` workflow and its `commit-message` pre-commit hook, both
pinned to the same release commit.
"""

import re
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
WORKFLOW = BASE_DIR / ".github" / "workflows" / "pull-request.yml"
PRE_COMMIT = BASE_DIR / ".pre-commit-config.yaml"
SHARED = "openpickleball/.github/.github/workflows/pull-request.yml"
SHARED_REPO = "https://github.com/openpickleball/.github"


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _shared_hooks_repo() -> dict[str, Any]:
    repos = [repo for repo in _load(PRE_COMMIT)["repos"] if repo["repo"] == SHARED_REPO]
    assert len(repos) == 1
    repo: dict[str, Any] = repos[0]
    return repo


def test_the_workflow_calls_the_shared_check_on_every_change() -> None:
    workflow = _load(WORKFLOW)
    # PyYAML reads the bare key `on` as True.
    events = workflow[True]["pull_request"]["types"]
    assert set(events) == {"opened", "edited", "synchronize", "reopened"}
    assert workflow["jobs"]["pull-request"]["uses"].split("@")[0] == SHARED


def test_the_workflow_and_the_hook_pin_the_same_commit() -> None:
    workflow_rev = _load(WORKFLOW)["jobs"]["pull-request"]["uses"].split("@")[1]
    assert re.fullmatch(r"[0-9a-f]{40}", workflow_rev)
    assert _shared_hooks_repo()["rev"] == workflow_rev


def test_the_pre_commit_config_installs_the_commit_msg_hook() -> None:
    config = _load(PRE_COMMIT)
    assert "commit-msg" in config["default_install_hook_types"]
    # Without this, every other hook would also run on the message file.
    assert config["default_stages"] == ["pre-commit"]
    hooks = [hook["id"] for hook in _shared_hooks_repo()["hooks"]]
    assert hooks == ["commit-message"]
