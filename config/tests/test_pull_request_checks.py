"""The pull request checks in scripts/check_pull_request.py.

The `pull-request` workflow runs them on every pull request, and the
pre-commit commit-msg hook runs them on each new commit message.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import check_pull_request as check

BASE_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPT = BASE_DIR / "scripts" / "check_pull_request.py"

GOOD = (
    "Import email tasks before checking their registration\n"
    "\n"
    "autodiscover_tasks() loads task modules lazily outside a worker.\n"
    "\n"
    "Closes #328\n"
    "\n"
    "Signed-off-by: A Contributor <contributor@example.com>\n"
)


def _problems(message: str) -> list[str]:
    return check.check_commit_message(message)


def test_a_good_message_passes() -> None:
    assert _problems(GOOD) == []
    assert check.check_commit_message(GOOD, require_sign_off=True) == []


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("", "subject line is empty"),
        ("A" * 73 + "\n\nWhy.", "73 characters"),
        ("test: fix the routing test\n\nWhy.", "type prefix"),
        ("fix(api)!: change it\n\nWhy.", "type prefix"),
        ("fixup! Add a thing\n\nWhy.", "squashed"),
        ("add a thing\n\nWhy.", "capital letter"),
        ("Add a thing.\n\nWhy.", "period"),
        ("Add a thing\nWhy on the second line.", "blank line"),
        ("Add a thing", "Add a body"),
        ("Add a thing\n\nSigned-off-by: A <a@example.com>", "Add a body"),
        ("Add a thing\n\nWhy \x07apps.", "shell escaping"),
    ],
)
def test_each_rule_reports_its_problem(message: str, expected: str) -> None:
    problems = _problems(message)
    assert any(expected in problem for problem in problems), problems


def test_the_hook_requires_a_sign_off() -> None:
    message = "Add a thing\n\nWhy it is needed."
    assert _problems(message) == []
    problems = check.check_commit_message(message, require_sign_off=True)
    assert problems == ["Sign off the commit: git commit -s."]


def test_merge_and_revert_messages_written_by_git_pass() -> None:
    assert _problems("Merge branch 'main' into issue-1-thing") == []
    revert = 'Revert "' + "A" * 80 + '"\n\nThis reverts commit abc1234.'
    assert _problems(revert) == []


def test_tab_and_crlf_are_not_control_problems() -> None:
    assert _problems("Add a thing\r\n\r\nWhy:\tbecause it is needed.\r\n") == []


@pytest.mark.parametrize(
    "body",
    [
        "Closes #328",
        "fixes #1",
        "Resolves: #12",
        "Refs #105 (left open)",
        "Part of #306.",
        "Closes openpickleball/backend#3",
        "Closes https://github.com/openpickleball/backend/issues/3",
    ],
)
def test_issue_links_are_accepted(body: str) -> None:
    assert check.check_description("Add a thing", f"Summary.\n\n{body}\n") == []


def test_a_description_without_an_issue_link_fails() -> None:
    problems = check.check_description("Add a thing", "See issue 328.")
    assert any("Link the issue" in problem for problem in problems)


def test_a_title_prefix_and_control_characters_fail() -> None:
    problems = check.check_description("test: fix it", "Fixes #328 in \x07pps")
    assert len(problems) == 2


@pytest.mark.parametrize(
    "body",
    [
        # From pull request 329: backticks typed in PowerShell.
        "Fixes #328\n\\\ttest_email_outbox_tasks_go_to_email\\ failed",
        "Fixes #328\nImport \\^Gpps.emails.tasks\\ first.",
    ],
)
def test_shell_escaping_damage_fails(body: str) -> None:
    problems = check.check_description("Import email tasks first", body)
    assert any("shell escaping" in problem for problem in problems), problems


def test_plain_tabs_and_markdown_escapes_pass() -> None:
    body = "Closes #1\n\n| a\t| b |\n\\*not a list\\*, `code`\n"
    assert check.check_description("Add a thing", body) == []


def test_api_pages_skip_merge_commits() -> None:
    page_one = [
        {"sha": "a" * 40, "parents": [{}], "commit": {"message": GOOD}},
        {"sha": "b" * 40, "parents": [{}, {}], "commit": {"message": "Merge"}},
    ]
    page_two = [{"sha": "c" * 40, "parents": [{}], "commit": {"message": "x"}}]
    pages = json.dumps(page_one) + "\n" + json.dumps(page_two) + "\n"
    commits = check.commits_from_api(pages)
    assert [commit.sha[0] for commit in commits] == ["a", "c"]


def test_a_pull_request_reports_each_commit_and_the_description() -> None:
    commits = [check.Commit("d717dfab" + "0" * 32, "test: fix it")]
    problems = check.check_pull_request(commits, "test: fix it", "", "someone")
    assert any(problem.startswith("Pull request: ") for problem in problems)
    assert any(
        problem.startswith("Commit d717dfa (test: fix it): ") for problem in problems
    )


def test_dependabot_pull_requests_are_exempt() -> None:
    commits = [check.Commit("a" * 40, "Bump redis from 8.1.0 to 8.2.0 " + "x" * 60)]
    assert check.check_pull_request(commits, "Bump redis", "", "dependabot[bot]") == []


def test_git_comments_and_the_scissors_diff_are_stripped() -> None:
    message = (
        "Add a thing\n\nWhy it is needed.\n\n"
        "# Please enter the commit message for your changes.\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/x b/x\n"
    )
    assert check.strip_git_comments(message) == "Add a thing\n\nWhy it is needed."


def _run(
    args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_the_commit_msg_command_exits_with_the_result(tmp_path: Path) -> None:
    good = tmp_path / "good"
    good.write_text(GOOD, encoding="utf-8")
    bad = tmp_path / "bad"
    bad.write_text("test: fix it\n", encoding="utf-8")
    assert _run(["commit-msg", str(good)]).returncode == 0
    result = _run(["commit-msg", str(bad)])
    assert result.returncode == 1
    assert "type prefix" in result.stdout


def test_the_pr_command_reads_the_environment(tmp_path: Path) -> None:
    commits = tmp_path / "commits.json"
    commits.write_text(
        json.dumps([{"sha": "a" * 40, "parents": [{}], "commit": {"message": GOOD}}]),
        encoding="utf-8",
    )
    env = {"PR_TITLE": "Add a thing", "PR_BODY": "Closes #1", "PR_AUTHOR": "someone"}
    assert _run(["pr", str(commits)], env).returncode == 0
    env["PR_BODY"] = "No link here"
    assert _run(["pr", str(commits)], env).returncode == 1


def test_main_passes_on_its_own_history() -> None:
    # Every commit on main from before this check follows its rules.
    log = subprocess.run(
        ["git", "log", "--no-merges", "-n", "200", "--format=%B%x00"],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if log.returncode != 0:
        pytest.skip("no git history")
    messages = [message.strip("\n") for message in log.stdout.split("\x00")]
    failing = {
        message.split("\n")[0]: _problems(message)
        for message in messages
        if message and _problems(message)
    }
    assert failing == {}


def test_the_workflow_runs_the_script_on_every_pull_request_change() -> None:
    workflow = yaml.safe_load(
        (BASE_DIR / ".github" / "workflows" / "pull-request.yml").read_text(
            encoding="utf-8"
        )
    )
    # PyYAML reads the bare key `on` as True.
    events = workflow[True]["pull_request"]["types"]
    assert set(events) == {"opened", "edited", "synchronize", "reopened"}
    steps = workflow["jobs"]["pull-request"]["steps"]
    run = "\n".join(step.get("run", "") for step in steps)
    assert "scripts/check_pull_request.py pr" in run
    # The title and body reach the script through the environment, never
    # pasted into the shell, where a crafted title could run commands.
    assert "github.event.pull_request.title" not in run
    assert "github.event.pull_request.body" not in run


def test_the_pre_commit_config_installs_the_commit_msg_hook() -> None:
    config = yaml.safe_load(
        (BASE_DIR / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    assert "commit-msg" in config["default_install_hook_types"]
    # Without this, every other hook would also run on the message file.
    assert config["default_stages"] == ["pre-commit"]
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    commit_msg = [hook for hook in hooks if hook["id"] == "commit-message"]
    assert commit_msg
    assert commit_msg[0]["stages"] == ["commit-msg"]
