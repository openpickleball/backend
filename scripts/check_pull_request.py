"""Check a pull request's commit messages and description.

CI runs this on every pull request (`.github/workflows/pull-request.yml`),
and the pre-commit `commit-msg` hook runs it on each new commit message.
It uses the standard library only, so it runs without `uv sync`.

    python3 scripts/check_pull_request.py pr COMMITS_JSON
    python3 scripts/check_pull_request.py commit-msg MESSAGE_FILE

`pr` reads the commits from a file holding the GitHub API's list of a
pull request's commits (`gh api --paginate repos/R/pulls/N/commits`, one
JSON array per page), and the title, body, and author from the
`PR_TITLE`, `PR_BODY`, and `PR_AUTHOR` environment variables. It skips
merge commits, as the `sign-off` check does, and pull requests opened by
Dependabot, whose titles and bodies it writes itself. The `sign-off`
check covers sign-offs in CI; `commit-msg` checks the sign-off too, so a
missing one is caught before the push.

Both exit non-zero and print each problem when any rule fails. The rules
are in CONTRIBUTING.md, "Commit messages" and "Branches and pull
requests".
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SUBJECT_MAX = 72
EXEMPT_AUTHORS = frozenset({"dependabot[bot]"})

# A Conventional Commits type, such as "test:", "fix(api):", or "feat!:".
TYPE_PREFIX = re.compile(r"[a-z]+(\([^)]*\))?!?:\s")
AUTOSQUASH = re.compile(r"(fixup|squash|amend)! ")
TRAILER = re.compile(r"[A-Za-z][A-Za-z-]*: .+")
SIGN_OFF = re.compile(r"Signed-off-by: .+ <[^<> ]+@[^<> ]+>\s*")
# Any control character other than tab, newline, and carriage return.
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# What a backtick in a double-quoted PowerShell string leaves once GitHub
# stores it: `t turns into a tab and `a into a bell, shown as ^G.
ESCAPE_DAMAGE = re.compile(r"\\\t|\^G")
ISSUE_LINK = re.compile(
    r"\b(close[sd]?|fix(e[sd])?|resolve[sd]?|refs|part of):?\s+"
    r"([\w.-]+/[\w.-]+#\d+|#\d+|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str


def _control_problem(text: str, where: str) -> list[str]:
    if CONTROL.search(text) or ESCAPE_DAMAGE.search(text):
        return [
            f"{where} looks damaged by shell escaping (a control character, a "
            "backslash before a tab, or ^G). A backtick in a double-quoted "
            "PowerShell string is an escape: edit the text on GitHub, or pass "
            "it with --body-file."
        ]
    return []


def check_commit_message(message: str, *, require_sign_off: bool = False) -> list[str]:
    """Return the problems with one commit message, or an empty list."""
    lines = message.replace("\r\n", "\n").rstrip("\n").split("\n")
    subject = lines[0].strip()
    problems = _control_problem(message, "The message")
    if not subject:
        return [*problems, "The subject line is empty."]
    if subject.startswith("Merge "):
        # A merge commit's message is written by git.
        return problems
    reverting = subject.startswith('Revert "')
    if len(subject) > SUBJECT_MAX and not reverting:
        problems.append(
            f"The subject is {len(subject)} characters; keep it to {SUBJECT_MAX}."
        )
    if AUTOSQUASH.match(subject):
        problems.append(
            "A fixup!, squash!, or amend! commit must be squashed before review: "
            "git rebase -i --autosquash."
        )
    elif TYPE_PREFIX.match(subject):
        problems.append(
            f'Drop the type prefix ("{subject.split(":")[0]}:"). '
            'Start with a verb, such as "Fix" or "Add".'
        )
    elif not subject[0].isupper():
        problems.append("Start the subject with a capital letter.")
    if subject.endswith("."):
        problems.append("Leave the period off the end of the subject.")
    if len(lines) > 1 and lines[1].strip():
        problems.append("Leave a blank line between the subject and the body.")
    body = [line for line in lines[1:] if line.strip()]
    if not any(not TRAILER.fullmatch(line.strip()) for line in body):
        problems.append(
            "Add a body that says why the change is needed, after a blank line."
        )
    if require_sign_off and not any(SIGN_OFF.fullmatch(line) for line in lines):
        problems.append("Sign off the commit: git commit -s.")
    return problems


def check_description(title: str, body: str) -> list[str]:
    """Return the problems with a pull request's title and body."""
    problems = _control_problem(title, "The title")
    problems += _control_problem(body, "The description")
    if TYPE_PREFIX.match(title.strip()):
        problems.append(
            f'Drop the type prefix ("{title.split(":")[0]}:") from the title.'
        )
    if not ISSUE_LINK.search(body):
        problems.append(
            "Link the issue in the description: Closes #N (or Refs #N or "
            "Part of #N when the issue stays open)."
        )
    return problems


def commits_from_api(pages: str) -> list[Commit]:
    """Read non-merge commits from `gh api --paginate` output."""
    decoder = json.JSONDecoder()
    commits: list[Commit] = []
    position = 0
    pages = pages.strip()
    while position < len(pages):
        page, position = decoder.raw_decode(pages, position)
        while position < len(pages) and pages[position].isspace():
            position += 1
        for item in page:
            if len(item["parents"]) < 2:
                commits.append(Commit(item["sha"], item["commit"]["message"]))
    return commits


def check_pull_request(
    commits: Iterable[Commit], title: str, body: str, author: str
) -> list[str]:
    """Return every problem in a pull request, each with its place."""
    if author in EXEMPT_AUTHORS:
        return []
    problems = [
        f"Pull request: {problem}" for problem in check_description(title, body)
    ]
    for commit in commits:
        subject = commit.message.split("\n", 1)[0]
        problems += [
            f"Commit {commit.sha[:7]} ({subject[:50]}): {problem}"
            for problem in check_commit_message(commit.message)
        ]
    return problems


def strip_git_comments(message: str) -> str:
    """Drop what git removes after the commit-msg hook runs.

    Git passes the hook the message before its cleanup, so the template's
    comment lines and the diff below the scissors line (`commit -v`) are
    still there.
    """
    kept = []
    for line in message.split("\n"):
        if line.startswith("# ") and ">8" in line and line.strip("# ->8") == "":
            break
        if not line.startswith("#"):
            kept.append(line)
    return "\n".join(kept).strip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pr").add_argument("commits", type=Path)
    commands.add_parser("commit-msg").add_argument("message", type=Path)
    args = parser.parse_args(argv)

    if args.command == "commit-msg":
        message = strip_git_comments(args.message.read_text(encoding="utf-8"))
        problems = check_commit_message(message, require_sign_off=True)
        fix = (
            "No commit was made. Git kept the message in COMMIT_EDITMSG in the "
            "git directory: fix it and commit again."
        )
    else:
        problems = check_pull_request(
            commits_from_api(args.commits.read_text(encoding="utf-8")),
            os.environ.get("PR_TITLE", ""),
            os.environ.get("PR_BODY", ""),
            os.environ.get("PR_AUTHOR", ""),
        )
        fix = (
            "Edit the title or description on GitHub, and reword commits with "
            "git rebase -i, then git push --force-with-lease. See CONTRIBUTING.md."
        )
    for problem in problems:
        print(problem)
    if problems:
        print(fix)
        return 1
    print("The commit messages and description follow the rules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
