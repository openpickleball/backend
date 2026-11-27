# Contributing to the OPEN backend

This guide covers the workflow for this repository. It adds to the [organization contributing guide](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md), which still applies: read it first. Report security problems privately, as described in the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md), never in a public issue.

To set up the project and run the tests, follow [Setup and tests](README.md#setup-and-tests) in the README.

## Two rules that are never negotiable

**No real player data, anywhere.** Do not put real player names, contact details, ratings, or match histories in code, fixtures, seed data, migrations, logs, issues, pull requests, or screenshots. Use made-up or properly de-identified data.

**OPEN stays free.** The [mission charter](https://github.com/openpickleball/.github/blob/main/CHARTER.md) rules out subscriptions, paid tiers, advertisements, and premium features. Do not add payment flows or anything that puts part of OPEN behind a payment. Players can always export their data.

## Pick up an issue

New here? Start with an issue labeled [`good first issue`](https://github.com/openpickleball/backend/issues?q=is%3Aissue%20is%3Aopen%20label%3A%22good%20first%20issue%22).

1. Skip an issue labeled `status:blocked`, or one whose "Blocked by" list still has an open issue. It cannot be finished yet.
2. Comment on the issue to say you are taking it, so two people do not do the same work. If someone already claimed it, pick another one or ask in the issue.
3. Read the issue in full. Many issues cite the backend plan's decisions as "decisions Qn" or "Fn". Those are in [issue #1](https://github.com/openpickleball/backend/issues/1): read the ones the issue names before you start.
4. Ask questions in the issue. A question early saves a rewrite later.

This repository links every pull request to an issue, so if none exists for your change, open one first. Pick the form that fits: a task for planned work, a bug report for something broken, or an epic to group related work. Each form asks for acceptance criteria, the checkable statements that say when the work is done.

## Branches and pull requests

1. Fork the repository, or, if you have write access, create a branch from an up-to-date `main`. Never commit to `main` in a fork you plan to open a pull request from.
2. Keep each pull request to one change. Split unrelated fixes into their own pull requests.
3. Link the issue in the pull request description with `Closes #N`, so it closes when the pull request merges. Use `Refs #N` or `Part of #N` when the issue stays open. The organization's pull request template starts the description for you.
4. Add or update tests and docs when behavior changes.
5. Keep the branch up to date with `main` before review.

A pull request merges only when these checks pass:

- `lint`: `ruff check`, `ruff format --check`, and the pre-commit hygiene hooks.
- `types`: `mypy .`.
- `test`: `manage.py check`, the missing-migrations check, the API schema check, and `pytest -n auto` (in parallel) with the coverage floor.
- `sign-off`: every commit carries a DCO sign-off.
- `pull-request / check`: the commit messages follow "Commit messages" below, the title has no type prefix, and the description links an issue and is not damaged by shell escaping. It runs again when you edit the title or description.

A maintainer reviews and merges every pull request.

## Sign off every commit

Every commit needs a [Developer Certificate of Origin](https://developercertificate.org/) sign-off. Add it with `-s`:

```bash
git commit -s -m "Describe your change"
```

If the `sign-off` check fails, add the missing sign-offs and push again. For the last commit only:

```bash
git commit --amend -s --no-edit
```

For every commit on your branch, fetch the current `main` and rebase onto it. In a fork, first add this repository as a remote once: `git remote add upstream https://github.com/openpickleball/backend.git`.

```bash
git fetch upstream && git rebase --signoff upstream/main
```

For a branch in this repository, use `git fetch origin && git rebase --signoff origin/main` instead.

Both rewrite your branch, so push with `git push --force-with-lease`. Only do this on your own branch.

## Commit messages

Follow the organization's [commit message rules](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md#commit-messages): an imperative subject of at most 72 characters with a capital letter, no type prefix such as `test:`, no period at the end, then a blank line and a body that says why. The `pull-request` check runs them on every pull request, through the organization's shared workflow.

`uv run pre-commit install` also installs the organization's `commit-message` hook, which checks the same rules and the sign-off on each commit before you push. If you installed the hooks before this rule, run it again to add the hook. To reword a commit that is already pushed, use `git rebase -i`, then `git push --force-with-lease`.

When you write a pull request description or commit message from a shell, pass it from a file (`gh pr create --body-file`, `git commit -F`). In a double-quoted PowerShell string, a backtick is an escape character, so Markdown code spans are lost.

## Code style

The tools are the source of truth for style. Do not argue formatting in review: run them.

- [ruff](https://docs.astral.sh/ruff/) for lint and formatting, configured in `pyproject.toml`.
- [mypy](https://mypy.readthedocs.io/) in strict mode with django-stubs, configured in `pyproject.toml`.
- [pre-commit](https://pre-commit.com/) runs both, plus hygiene checks, on every commit. See `.pre-commit-config.yaml`.
- `.editorconfig` sets encoding and whitespace defaults for your editor: UTF-8, LF line endings, a final newline, no trailing spaces, and the indent size for Python, YAML, and TOML files. Its Python settings match ruff format.

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

The README section [Lint, types, and git hooks](README.md#lint-types-and-git-hooks) explains what mypy needs to run. If a rule can be checked by ruff, enable the ruff rule instead of adding it here. These conventions are the ones a tool cannot check:

- **Names come from [`CONTEXT.md`](CONTEXT.md).** Use its terms for models, endpoints, fields, tests, and docs: Player, Account, Match, Organizer, and so on. Say "player", "match", and "organizer", not "user" or "entity". If you need a new term, add it to `CONTEXT.md` in the same pull request.
- **Apps live under `apps/`.** Each first-party Django app is a package in `apps/<app>/`, with its tests in `apps/<app>/tests/` and its API operations on a `Router` in `apps/<app>/api.py`. The `config/api.py` docstring shows how to mount a router.
- **Write an ADR for decisions that are hard to reverse.** [`docs/adr/README.md`](docs/adr/README.md) says when to write one and gives the template. Check the existing records before changing a decision they cover.

## Tests

Every change to behavior needs tests that would fail without it. A bug fix needs a test that reproduces the bug.

- Write tests with [pytest](https://docs.pytest.org/): plain functions, plain `assert`, `pytest.raises`, and fixtures. Do not use `self.assert*` or `assertRaises`. Ruff's pytest-style rules (`PT`) check this. Some older files are exempt until issue #146 converts them.
- Build test records with the [factory_boy](https://factoryboy.readthedocs.io/) factories, such as `UserFactory` in `apps/accounts/tests/factories.py`. They fill in made-up [Faker](https://faker.readthedocs.io/) data. When you add a model, consider adding a factory for it.
- Tests run against Postgres, not SQLite. Start the local services as the README describes before you run them.
- `uv run pytest` fails when total coverage drops below `fail_under` in `pyproject.toml`. Do not lower the floor to make a pull request pass.
- The floor counts all of `apps` and `config`, so a narrow run (one file, or `-k`) usually fails it. Add `--no-cov` to those runs:

```bash
uv run pytest --no-cov apps/accounts
```

- `uv run pytest -n auto` runs the suite in parallel with pytest-xdist, one worker per CPU, as CI does. Each worker gets its own test database, so tests must not depend on running in one process or in a set order.

## Migrations

- Generate migrations with `uv run python manage.py makemigrations`. Never write one by hand. Where a generated migration needs a line makemigrations cannot write, such as the `run_before` in `apps/partners/migrations/0001_initial.py` (ADR 0010), add only that line, with a comment saying why.
- Aim for one migration per app per pull request. If you change a model again before your pull request merges, delete your unmerged migration and generate it again, instead of stacking a second one on top. Before you delete it, migrate your local database back with `uv run python manage.py migrate <app> <previous migration>`, so the database keeps no record of a migration that no longer exists. If it is the app's first migration, use `zero` in place of the previous migration.
- Never edit or delete a migration that has merged to `main`. Other databases have already applied it. Change the schema with a new migration.
- CI runs `makemigrations --check --dry-run` and fails when a model change has no migration. Run it yourself before you push:

```bash
uv run python manage.py makemigrations --check --dry-run
```

## Supply chain checks

- Install only from the lock file. CI runs `uv sync --locked`, which fails when `uv.lock` is out of step with `pyproject.toml` and checks each package against its hash in `uv.lock`. The production image installs the same way. After changing dependencies, run `uv lock` and commit both files.
- The `audit` workflow (`.github/workflows/audit.yml`) runs `scripts/audit_dependencies.py` on every pull request, every push to `main`, and every Monday. Its `audit` job is meant to be a required check on `main` too. It runs [pip-audit](https://pypi.org/project/pip-audit/) against every locked package, dev packages included, and fails on any known vulnerability. Run it yourself with:

```bash
uv run python scripts/audit_dependencies.py
```

- The audit also fails when a package in `uv.lock` does not come from PyPI. The engine is the one exception: it is installed from a git tag, so the lock file holds its commit but no artifact hash, and no vulnerability database tracks it. The audit checks instead that it is pinned to a version tag (such as `v1.0.0`) and a full commit SHA. Dependabot leaves it alone: upgrade it as [docs/engine.md](docs/engine.md) describes.
- Dependabot (`.github/dependabot.yml`) opens weekly pull requests for Python packages, GitHub Actions, and the Dockerfile's base images, with minor and patch updates grouped. It keeps the Python minor version that `.python-version` names. Secret scanning, push protection, and Dependabot alerts are turned on in the repository settings, so a push that contains a known secret pattern is refused.
- Every action in a workflow is pinned to a full commit SHA, with its version in a comment, and every workflow sets least-privilege `permissions:`. A test (`config/tests/test_supply_chain.py`) checks both.

### When the audit or an alert reports a vulnerability

1. Upgrade the package to a fixed version (`uv lock --upgrade-package <name>`), run the tests, and open a pull request. A Dependabot security update may already have opened one.
2. If no fixed version exists, or the fix cannot land yet, and the vulnerability does not affect OPEN (the vulnerable code is never called, say), a maintainer may accept it. Add an entry to `.github/pip-audit-exceptions.toml` with the advisory id, the package, the reason, and an expiry date. The exception covers that advisory in that package only, and the audit fails once the package leaves `uv.lock`. The audit fails once that date passes, so the exception is reviewed again.
3. A vulnerability that does affect OPEN is a security problem: report it privately, as the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md) describes, not in a public issue or pull request.
4. If push protection blocks a push, remove the secret from the commits before pushing again. If a real secret was ever pushed, treat it as leaked: revoke it and tell the maintainers privately.

## API schema

- `openapi/openapi.json` is the API's OpenAPI document. A command generates it. Never edit it by hand.
- Regenerate it whenever you change an operation, a schema, or the API's title, version, or description, and commit it with the change:

```bash
uv run python manage.py export_openapi_schema
```

- Run the export and the check with `DJANGO_DEBUG` on, as `.env.example` sets it. With it off, the document names the session cookie `__Host-sessionid`, so the export rewrites the file and the check fails.
- CI runs `export_openapi_schema --check` and fails when the committed file differs from the API. The test suite checks it too. Run the check yourself before you push:

```bash
uv run python manage.py export_openapi_schema --check
```

- Each published GitHub release carries the file as the asset `openapi.json`. The published Apache-2.0 copy of the API specification will live in the `partner-docs` repository, not here.
