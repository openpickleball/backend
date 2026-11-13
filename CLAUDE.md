# CLAUDE.md

This file gives Claude Code guidance for working in this repository.

## What this repository is

The backend for OPEN, the Open Pickleball Evaluation Network: a free, open-source, community-governed pickleball rating network. This repository will hold the services and APIs that store players and matches and serve ratings. The rating math itself lives in the separate `engine` repository.

Planned services, from the project roadmap:

- **Results API:** platforms, clubs, and tournaments submit matches.
- **Ratings and stats API:** any app can read OPEN ratings, history, and stats for free.
- **Organizer tools (backend side):** upload results, confirm starting ratings, review flagged matches.

## Current status

The Django project scaffold exists (`config`, with settings read from the environment, plus `apps/accounts` with the custom email-login user model and the first migration, `config/api.py` with the django-ninja API mounted at `/api/v1/` but no operations yet, and `compose.yaml` for local services), and no OPEN rating is live. The work is planned as GitHub issues grouped into outcome milestones (v0.1 MVP through v0.4), epics with sub-issues, native blocked-by links, and the "OPEN backend" org project board; `docs/backlog-plan.md` explains the order and the critical path. Issue #1 records the plan's decisions and open questions; other issues cite them as "decisions Qn" or "Fn". Read the relevant ones before starting an issue.

## Stack (decided, see issue #1)

- Python 3.13, managed with uv (`uv.lock` committed)
- Django 5.2 LTS (`Django>=5.2,<5.3`, decision Q1), project package `config`, first-party apps under `apps/`
- django-ninja for the API, mounted at `/api/v1/` with OpenAPI docs; every operation has an explicit `operation_id`
- Postgres (psycopg 3), Valkey as cache and Celery broker (decision Q11)
- Celery with django-celery-beat; queues `default`, `ratings`, `email`, `webhooks`
- Settings from environment variables via django-environ; `.env.example` holds placeholders only
- pytest, pytest-django, pytest-cov, factory_boy; ruff, mypy with django-stubs, pre-commit
- Deployed on DigitalOcean App Platform (M3)

## Ordering rules

- `AUTH_USER_MODEL` is `accounts.User` (issue #3) and is fixed: never swap it. Other apps reference it through `settings.AUTH_USER_MODEL` for foreign keys and `get_user_model()` in code, never by importing `apps.accounts.models.User`.
- The custom OAuth Application model (issue #22) lands before django-oauth-toolkit's migrations are run (decision Q3).
- The engine is pinned as a git dependency on a tag (decision Q4). It is a demonstration package, so its output stays labeled as demonstration ratings.

## Account emails

- An Account's email is its login. Forms (through `clean()`) and the manager normalize it: NFKC, strip, then lowercase one character at a time.
- The normalized email must be ASCII, at most 254 characters, and a valid address. The field validator and the manager enforce these rules. Keep every new check on the normalized value, not the raw input.
- The database enforces the column length, case-insensitive uniqueness (`Lower("email")`), and ASCII. It does not normalize. Raw writes (`save()` without `full_clean()`, `bulk_create`, `update`) skip normalization, so create Accounts through the manager or a form.
- Find an Account by email (an identity lookup, such as login or linking) only through `get_user_model().objects.get_by_natural_key()` (or `aget_by_natural_key()`), which compares with the unique constraint's own `Lower("email")` expression. Do not use `email__iexact` or an exact match on raw input: on Postgres `iexact` compares `UPPER()`, which can match accounts the constraint treats as distinct.

## API

- There is one `NinjaAPI`, `api` in `config/api.py`, mounted at `/api/v1/`. Each app adds its operations to a `Router` in `apps/<app>/api.py`, and `config/api.py` mounts that router. Its module docstring shows how.
- Every operation passes an explicit `operation_id`. It names the method in the generated client, so never change one for a rename. Building the OpenAPI document raises an error for an operation in the document that has none, and a test checks every operation.
- Validation errors, an unparsable body, `Http404`, and `HttpError` come back as `ErrorResponse` (`config/api_errors.py`), and validation errors return 422. Any other exception gives Django's 500 when DEBUG is off. Raise `HttpError` or `Http404` instead of returning an error body by hand. Never declare a different 422 schema on an operation that takes input: building the document raises an error if you do. ADR 0005 records the versioning and error format.
- Never put the submitted value in a validation message. The 422 response returns the message to the client.

## Settings

- `config/settings.py` reads its deployment values from the environment (and an optional `.env`). Read a new variable with the helpers there (`_raw`, `_required`, `_list`), not django-environ's `Env` getters: those treat a value starting with `$` as a reference to another variable, which silently swaps secrets and URLs.
- The errors that `config/settings.py` raises for a bad or missing variable name the variable and never echo its value. django-environ's own `.env` reader is the exception: it logs a malformed `.env` line in full.
- With DEBUG off, settings turn on the https redirect, secure cookies, and HSTS (see ADR 0004). The test run turns off the redirect and swaps the cache to LocMemCache (so tests need no cache server). pytest gets these overrides from `config/settings_test.py`, and `manage.py test` gets them from `config/test_runner.py`. Keep the two sets identical (a test fails if they drift). pytest's set lives in the settings module, so every process that loads it, parallel workers included, gets it; the runner applies its set in spawned parallel workers through `process_setup` (forked workers inherit it).
- `REDIS_URL` takes only the form `redis://[USER:PASSWORD@]HOST[:PORT][/DB]`, or `rediss://` for TLS: a lowercase scheme, a numeric database, and no query string or fragment. Settings refuse at startup a URL that breaks these rules. redis-py lets a query string override settings, certificate verification included, so set connection and TLS options in settings, never in the URL. Percent-encode any URL-reserved character in the password.
- Email prints to the console when DEBUG is on and is discarded when DEBUG is off, until #35 adds a real provider. Never let a DEBUG-off environment print or log email: messages carry recipient addresses and, once auth lands, reset tokens.

## Commands

Fill these in as the M0 issues land. A command for a tool or service that has not landed yet fails until its issue does. Every command that loads settings needs the settings variables. That includes `check`, `mypy`, and the pre-commit mypy hook (the django-stubs plugin imports `config.settings`). Copy `.env.example` to `.env` first; process environment values win over it. `migrate`, the tests, and `runserver` (which checks migrations at startup) need the Postgres that `DATABASE_URL` names. `docker compose up -d --wait` starts it and Valkey from `compose.yaml` and waits until both are healthy; the `.env.example` URLs point at them. Docker Compose also reads `.env` and treats a `$` in a value as a variable reference, so wrap a secret key that contains `$` in single quotes. `uv run pytest` runs the suite with coverage and fails when total coverage is below `fail_under` in `pyproject.toml`. The floor counts all of `apps` and `config`, so a narrow run (one file or a `-k` selection) usually fails it even when every test passes: add `--no-cov` to those runs. To build test records, prefer the factory_boy factories (such as `UserFactory` in `apps/accounts/tests/factories.py`), which fill in made-up Faker data. Expected shape:

```bash
cp .env.example .env                      # local settings; never commit .env
uv sync                                   # install from uv.lock
uv run pre-commit install                 # git hooks: ruff, mypy, hygiene checks
uv run python manage.py check             # Django system checks, no database needed
docker compose up -d --wait               # local Postgres and Valkey, healthy
uv run python manage.py migrate
uv run python manage.py runserver
docker compose --profile web up -d --wait # optional: the app in a container on 127.0.0.1:8000, instead of runserver on the host
docker compose down -v                    # stop and delete local data
uv run pytest                             # tests with coverage (Postgres, not SQLite)
uv run pytest --no-cov apps/accounts      # part of the suite, without the coverage floor
uv run ruff check . && uv run ruff format --check .
uv run mypy .                             # strict; needs the settings variables
uv run python manage.py makemigrations --check --dry-run   # CI fails on missing migrations
uv run pre-commit run --all-files
```

`.github/workflows/ci.yml` runs on every pull request and every push to `main`, in three jobs: `lint` (`ruff check`, `ruff format --check`, and the pre-commit hygiene hooks), `types` (`mypy .`), and `test` (`manage.py check`, `makemigrations --check --dry-run`, and `pytest` with the coverage floor, against a Postgres service). The job names are meant to be required status checks on `main` alongside `sign-off`, so a change that renames a job must also update the `main` ruleset.

## Documentation

- `README.md`: public overview, setup, and contribution pointers.
- `CONTEXT.md`: the domain language (Match, Ledger, Replay, Rating Format, Reliability, and so on). Read it before naming models, endpoints, or fields.
- `docs/adr/`: architecture decision records. Check here before changing a decision, and add a record when you make one.
- GitHub issue #1: the backend plan's decisions (Q1 to Q16, F1 to F11) and checked package versions.
- Organization-wide policy lives in the `openpickleball/.github` repository: charter, governance, roadmap, contributing guide, security policy, licensing. A local checkout may exist at `../OPEN-pickleball`.
- The reference engine may be checked out at `../OPEN-engine`. Its module docstring in `src/open_engine/engine.py` lists production requirements the backend must meet.

## Hard rules

1. **No real player data, anywhere.** Not in code, fixtures, seed scripts, migrations, logs, issues, pull requests, or screenshots. Use made-up or properly de-identified data. `.gitignore` excludes local data directories and database files; keep it that way.
2. **Do not reimplement rating math.** The backend calls the engine. If a calculation seems wrong or missing, the fix belongs in the engine or the rating spec, not here.
3. **The match ledger is append-only.** Corrections are new records that reference the old one, never in-place edits or deletes of counted matches. Authoritative ratings come from a deterministic replay of the ledger; live updates between replays are provisional and must be labeled as such.
4. **Store replay provenance.** Persist each replay's `as_of` date, config version, and config fingerprint so any published rating can be reproduced.
5. **External ratings are a seed only.** An imported rating initializes a player and needs verified identity and evidence. It can never overwrite an OPEN-earned rating once OPEN matches exist. Show imported reliability separately from OPEN-earned reliability.
6. **Do not name competing rating systems** in code, docs, issues, or comments. Say "standard external rating system" or "external rating".
7. **Charter limits.** No paid tiers, premium features, ads, or payment flows. Players can always export their data. Do not build anything that conflicts with the [mission charter](https://github.com/openpickleball/.github/blob/main/CHARTER.md).
8. **Security-sensitive areas:** score confirmation, organizer verification, duplicate detection, identity linking, and anything that lets one party change another player's rating. Treat bypasses here as security bugs, not feature gaps.

## Contribution conventions

- Every commit needs a DCO sign-off (`git commit -s`). CI (`.github/workflows/dco.yml`) rejects pull requests with unsigned commits. That check runs only on pull requests, so commits pushed straight to `main` must be signed off by hand.
- Founding phase: repository admins may push directly to `main` while the project is getting established. Everyone else, and admins once this phase ends, uses one change per pull request, linked to an issue. Add or update tests and docs when behavior changes.
- License: AGPL-3.0-or-later for this repository. API specifications are Apache-2.0 and published separately, so do not place OpenAPI specs here as the canonical copy.
- Before proposing a feature, ask whether it makes the standard more accurate, more trusted, or easier to adopt. If not, it probably belongs in a partner's product.

## Git exception

This section overrides a contributor's global no-commit rule for the `/work-issue` workflow only.

- The workflow may commit (`git commit -s`, with explicit paths) and push, but only on the `issue-<N>-*` branch it creates or resumes.
- After a rebase it may push that branch with `--force-with-lease`. It never force-pushes any other branch.
- It may open a pull request against `main`, edit its description, label it (creating the ready label if it does not exist), and reply to review comments on it.
- It never commits or pushes to `main`, never merges, and never enables auto-merge. A human reviews and merges every pull request.

## Writing style

Docs in this project use plain, short sentences. No emojis and no em dashes. Prefer "player", "match", and "organizer" over generic words like "user" or "entity" (see `CONTEXT.md`).
