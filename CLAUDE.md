# CLAUDE.md

This file gives Claude Code guidance for working in this repository.

## What this repository is

The backend for OPEN, the Open Pickleball Evaluation Network: a free, open-source, community-governed pickleball rating network. This repository will hold the services and APIs that store players and matches and serve ratings. The rating math itself lives in the separate `engine` repository.

Planned services, from the project roadmap:

- **Results API:** platforms, clubs, and tournaments submit matches.
- **Ratings and stats API:** any app can read OPEN ratings, history, and stats for free.
- **Organizer tools (backend side):** upload results, confirm starting ratings, review flagged matches.

## Current status

The Django project, its apps under `apps/`, the django-ninja API at `/api/v1/`, allauth's headless auth at `/_allauth/`, Celery, and the App Platform deploy exist. The full inventory of what has landed, module by module, is in `.claude/rules/current-status.md`. No OPEN rating is published: the rating operations return demonstration ratings to signed-in Accounts only (ADR 0031). The work is planned as GitHub issues grouped into outcome milestones (v0.1 MVP through v0.4), epics with sub-issues, native blocked-by links, and the "OPEN backend" org project board. Issue #1 records the plan's decisions and open questions; other issues cite them as "decisions Qn" or "Fn". Read the relevant ones before starting an issue.

## Topic rules

The detailed rules for each area live in `.claude/rules/`, one file per section, each loaded when you work on the files its `paths` list names. A reference to a CLAUDE.md section in code or docs ("CLAUDE.md, API") means the section of that name, here or in its rules file. Read the file for an area before changing it, even when it did not load on its own.

- "Authentication": `.claude/rules/authentication.md`
- "Audit log": `.claude/rules/audit-log.md`
- "Email": `.claude/rules/email.md`
- "Players": `.claude/rules/players.md`
- "Account deletion": `.claude/rules/account-deletion.md`
- "Data export": `.claude/rules/data-export.md`
- "Clubs and organizers": `.claude/rules/clubs-and-organizers.md`
- "Events": `.claude/rules/events.md`
- "Matches": `.claude/rules/matches.md`
- "Ratings": `.claude/rules/ratings.md`
- "Partner applications": `.claude/rules/partner-applications.md`
- "API": `.claude/rules/api.md`
- "Settings": `.claude/rules/settings.md`
- "Current status" (full inventory): `.claude/rules/current-status.md`

Keep this file under 40,000 characters: put a new area's rules in its own rules file with a `paths` list, and add it here.

## Stack (decided, see issue #1)

- Python 3.13, managed with uv (`uv.lock` committed)
- Django 5.2 LTS (`Django>=5.2,<5.3`, decision Q1), project package `config`, first-party apps under `apps/`
- django-ninja for the API, mounted at `/api/v1/` with OpenAPI docs; every operation has an explicit `operation_id`
- Postgres (psycopg 3), Valkey as cache and Celery broker (decision Q11)
- Celery with django-celery-beat; queues `default`, `ratings`, `email`, `webhooks`
- Settings from environment variables via django-environ; `.env.example` holds placeholders only
- pytest, pytest-django, pytest-cov, pytest-xdist, factory_boy; ruff, mypy with django-stubs, pre-commit
- gunicorn serves the app (`gunicorn.conf.py`), WhiteNoise serves static files, django-cors-headers handles CORS
- django-allauth 65 in headless mode for Account sign-up, login, email verification, and password reset (ADR 0014), no social login
- django-oauth-toolkit 3.4 for partner OAuth2 (decisions Q2, Q3), with local type stubs in `typings/oauth2_provider` (`mypy_path`)
- django-anymail 15 with the Resend extra for outgoing email (ADR 0016); only the Resend extra is installed
- django-csp 4 for the Content Security Policy (ADR 0040), in its 4.x settings format (`CONTENT_SECURITY_POLICY` dicts); its system check refuses the old `CSP_*` settings, so no Django setting may be named `CSP_REPORT_ONLY`
- Deployed on DigitalOcean App Platform (M3)

## Ordering rules

- `AUTH_USER_MODEL` is `accounts.User` (issue #3) and is fixed: never swap it. Other apps reference it through `settings.AUTH_USER_MODEL` for foreign keys and `get_user_model()` in code, never by importing `apps.accounts.models.User`.
- `OAUTH2_PROVIDER_APPLICATION_MODEL` is `partners.PartnerApplication` (issue #22, decision Q3) and is fixed like `AUTH_USER_MODEL`: never swap it. Outside `apps/partners`, reference it through `settings.OAUTH2_PROVIDER_APPLICATION_MODEL` for foreign keys and `oauth2_provider.models.get_application_model()` in code (a type-only import under `TYPE_CHECKING` is fine). django-oauth-toolkit's `0001_initial` has foreign keys to that model without depending on it, so `apps/partners/migrations/0001_initial.py` carries `run_before = [("oauth2_provider", "0001_initial")]`, a hand edit to a generated migration (ADR 0010). A test pins the order.
- The engine is pinned as a git dependency on a tag (decision Q4). It is a demonstration package, so its output stays labeled as demonstration ratings.

## Account emails

- An Account's email is its login. Forms (through `clean()`) and the manager normalize it: NFKC, strip, then lowercase one character at a time.
- The normalized email must be ASCII, at most 254 characters, and a valid address. The field validator and the manager enforce these rules. Keep every new check on the normalized value, not the raw input.
- The database enforces the column length, case-insensitive uniqueness (`Lower("email")`), and ASCII. It does not normalize. Raw writes (`save()` without `full_clean()`, `bulk_create`, `update`) skip normalization, so create Accounts through the manager or a form.
- Find an Account by email (an identity lookup, such as login or linking) only through `get_user_model().objects.get_by_natural_key()` (or `aget_by_natural_key()`), which compares with the unique constraint's own `Lower("email")` expression. Do not use `email__iexact` or an exact match on raw input: on Postgres `iexact` compares `UPPER()`, which can match accounts the constraint treats as distinct.

## Public identifiers

- A model whose records have a public identity (Player, Club, Event, Organizer, Match, Results File) inherits `apps.public_ids.models.PublicCodeModel` and sets `public_code_type`. The system check `public_ids.E001` reports a concrete subclass without one. ADR 0008 records the scheme.
- Codes are created only by `save()` (or `objects.create()`), which issues the code, records it in `IssuedCode`, and refuses to change it later. Fixtures (`loaddata`), `bulk_create`, `QuerySet.update`, and raw SQL skip all of this and must not set codes. To copy a record, set `public_code = None`, `pk = None`, and `_state.adding = True` before saving. Saving a deleted record raises.
- Never expose or accept a row id in the API. A path parameter that names a record is `Annotated[str, PublicCodePath(CodeType.X)]`, and the view looks the record up with `get_by_public_code()`. `PublicCodePath` is for path parameters only, not body or response fields. A test fails on any numeric `id`, `pk`, `*_id`, `*Id`, or plural of these in the OpenAPI document, hidden operations included. allauth's emailed keys are the one exception (see Authentication).
- To read a code from other input (such as a CSV column), use `apps.public_ids.codes.normalize()`, then look up the canonical form.

## Personal data

- ADR 0009 sets what OPEN collects. Every model field that holds personal data is listed in `docs/privacy/personal-data-inventory.md`. Add, change, or remove its entry in the same pull request as the field.
- Never store a full birthdate (store birth year and month only), a raw gender, a street address, a phone number, or payment data.
- A Player's legal name, birth year and month, and gender division, the data export's JSON (`exports.DataExport.content`, ADR 0051), and allauth's TOTP secrets and recovery code seeds are encrypted with the keys in `FIELD_ENCRYPTION_KEYS` (ADR 0046). The MFA secrets go through OPEN's MFA adapter (`MFA_ADAPTER`, `apps/accounts/mfa_adapter.py`), whose `decrypt()` never falls back to plain text. The pending TOTP secret and the signup hold in the session stay plain text. A new field that must be encrypted uses `EncryptedCharField`, `EncryptedTextField`, or `EncryptedPositiveSmallIntegerField` from `apps.encryption.fields` (no other encryption code), adds a data migration for its stored values, and is listed in the inventory as encrypted.
- `config/tests/test_personal_data_inventory.py` fails when an installed model is not named in the inventory by its label (`` `app_label.ModelName` ``), or a first-party model's field (other than `id`) is not named on a line that names its model or as `` `app_label.ModelName.field` ``. Name every new model and field there, personal data or not.
- Only the player sets their own display name, legal name, birth year and month, gender division, and city and region. The birth year and month and the gender division are set once, at signup or when the Player is created, and the player cannot change them after that (ADR 0023). Others may only remove them (#190, #116).
- Every path that creates a Player refuses anyone under 13, and a new Player always has a birth year and month and a gender division (`mens` or `womens`, ADR 0019). `Player.save()` still allows an empty gender division, for removal (#190, Account deletion); the database checks nothing about it (ADR 0046). The birth year and month columns allow NULL only for Account deletion: `Player.save()` refuses a new Player without them (ADR 0048). Players aged 13 to 17 are left out of all partner sharing. A Player whose birth year and month were removed (#190, #116) is excluded from all partner sharing, permanently.
- The identifying personal data fields are email, display name, legal name, birth year and month, gender division, city and region, an Organizer's name (`clubs.Organizer.name`), External IDs (#49), and platform player IDs (#82). An Audit Event about one of them records the field's name, never its value. Account deletion is wider: it removes all personal data except what ADR 0009's Retention section keeps.
- No model's `__str__` returns an identifying field, so the admin log's `object_repr` and admin messages never hold one (a Player's, a Club's, an Organizer's, an Event's, and a Match's return their OPEN Code, a Match Participant's and a Game's their Match's OPEN Code with the team or game number, and a Partner Application's its `client_id`). allauth's `EmailAddress.__str__` returns the email, so its admin is unregistered. `accounts.User.__str__` returns the email, the known exception: Account deletion deletes the admin log entries tied to the Account (ADR 0048).

## Commands

Fill these in as the M0 issues land. A command for a tool or service that has not landed yet fails until its issue does. Every command that loads settings needs the settings variables. That includes `check`, `mypy`, and the pre-commit mypy hook (the django-stubs plugin imports `config.settings`). Copy `.env.example` to `.env` first; process environment values win over it. `migrate`, the tests, `runserver` (which checks migrations at startup), and `celery beat` (which needs the migrations applied) need the Postgres that `DATABASE_URL` names; in pooled mode `migrate` uses the one `DIRECT_DATABASE_URL` names (ADR 0038). A Celery worker needs the Valkey that `REDIS_URL` names. `docker compose up -d --wait` starts it and Valkey from `compose.yaml` and waits until both are healthy; the `.env.example` URLs point at them. Docker Compose also reads `.env` and treats a `$` in a value as a variable reference, so wrap a secret key that contains `$` in single quotes. `uv run pytest` runs the suite with coverage and fails when total coverage is below `fail_under` in `pyproject.toml`. The floor counts all of `apps` and `config`, so a narrow run (one file or a `-k` selection) usually fails it even when every test passes: add `--no-cov` to those runs. To build test records, prefer the factory_boy factories (such as `UserFactory` in `apps/accounts/tests/factories.py`), which fill in made-up Faker data. Expected shape:

```bash
cp .env.example .env                      # local settings; never commit .env; set FIELD_ENCRYPTION_KEYS to a generated key
uv sync                                   # install from uv.lock
uv run pre-commit install                 # git hooks: ruff, mypy, hygiene checks
uv run python manage.py check             # Django system checks, no database needed
docker compose up -d --wait               # local Postgres and Valkey, healthy
uv run python manage.py migrate
uv run python manage.py runserver
DJANGO_DEBUG=true uv run python manage.py seed_dev_data --players 20   # made-up dev data; DEBUG only; reset with down -v
uv run celery -A config worker -Q ratings,email,webhooks,default -l info   # background tasks, every queue
uv run celery -A config beat -l info      # periodic tasks; run one beat only, after migrate
uv run python manage.py collectstatic --noinput   # before serving with DEBUG off
uv run python manage.py reencrypt_fields          # after a new key is put first in FIELD_ENCRYPTION_KEYS; prints counts only
uv run gunicorn                           # serve with gunicorn.conf.py (PORT, WEB_CONCURRENCY)
docker build -t open-backend:dev .        # production image: gunicorn by default; override the command for migrate, worker, beat
docker compose --profile web up -d --wait # optional: the app, a worker, and beat in containers, instead of on the host (beat waits for migrate)
docker compose --profile pgbouncer up -d --wait   # optional: PgBouncer in transaction mode on 127.0.0.1:6432; set DATABASE_URL to it and DIRECT_DATABASE_URL to db
docker compose down -v                    # stop and delete local data
uv run pytest                             # tests with coverage (Postgres, not SQLite)
uv run pytest -n auto                     # the same in parallel, one worker per CPU, as CI runs it
uv run pytest --no-cov apps/accounts      # part of the suite, without the coverage floor
uv run ruff check . && uv run ruff format --check .
uv run mypy .                             # strict; needs the settings variables
uv run python manage.py makemigrations --check --dry-run   # CI fails on missing migrations
uv run python manage.py export_openapi_schema              # regenerate openapi/openapi.json after an API change
uv run python manage.py export_openapi_schema --check      # CI fails when openapi/openapi.json is stale
uv run python scripts/audit_dependencies.py               # pip-audit on every locked package; CI fails on a known vulnerability
uv run pre-commit run --all-files
```

`.github/workflows/ci.yml` runs on every pull request and every push to `main`, in four jobs: `lint` (`ruff check`, `ruff format --check`, and the pre-commit hygiene hooks), `types` (`mypy .`), `test` (`manage.py check`, `check --deploy --fail-level WARNING` with made-up production-like values, `makemigrations --check --dry-run`, `export_openapi_schema --check`, and `pytest -n auto` with the coverage floor, against a Postgres service), and `image` (builds the `Dockerfile` with a planted `.env` and local database, then checks that the image runs as a non-root user, holds neither of them and no uv, git, or dev package, carries `LICENSE`, has the collected static files, and passes `manage.py check`). `.github/workflows/audit.yml` runs its `audit` job (`scripts/audit_dependencies.py`) on every pull request, every push to `main`, and weekly. The job names are meant to be required status checks on `main` alongside `sign-off`, so a change that renames a job must also update the `main` ruleset.

## Documentation

- `README.md`: public overview, setup, and contribution pointers.
- `CONTRIBUTING.md`: this repository's contribution workflow (pull requests, DCO, code style, tests, migrations, the API schema, good first issues). It adds to the org guide.
- `CONTEXT.md`: the domain language (Match, Ledger, Replay, Rating Format, Reliability, and so on). Read it before naming models, endpoints, or fields.
- `docs/adr/`: architecture decision records. Check here before changing a decision, and add a record when you make one.
- `docs/privacy/personal-data-inventory.md`: every personal data field, with purpose, visibility, retention, and encryption (ADR 0009).
- `docs/engine.md`: the engine pin, how to read the installed engine version, and the upgrade procedure.
- `docs/email.md`: the sending domain `mail.openpb.org`, its Resend setup and DNS records (SPF, DKIM, DMARC), and the DMARC rollout.
- `docs/data-export.md`: the player data export format (#39, ADR 0051), the file `download_data_export` returns.
- `docs/csv-format.md`: the CSV results format organizers upload (#70, #71), with its column-to-field map and common errors. `docs/csv-results-template.csv` is its template, with made-up players.
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
- Supply chain (#104, `CONTRIBUTING.md` "Supply chain checks"): every `uv sync` in a workflow or the Dockerfile passes `--locked`, every action is pinned to a full commit SHA with its version in a comment, and every workflow sets top-level `permissions:` that only read; `config/tests/test_supply_chain.py` checks these. Every package in `uv.lock` comes from PyPI, except the engine, pinned to a version tag and a full commit; the audit fails otherwise. An accepted vulnerability goes in `.github/pip-audit-exceptions.toml` with a reason and an expiry date, never as a pip-audit flag in the workflow.
- `.github/CODEOWNERS` makes the maintainers team the owner of every path. When an app for a security-sensitive area (hard rule 8) or the match ledger lands, give its directory its own entry, owned by the maintainers team, in the same pull request. A test requires every CODEOWNERS path to match a tracked file or a directory that holds one.
- The `.editorconfig` Python settings (indent and line length) must agree with ruff format. A test compares them with ruff's resolved settings, so change both together.
- License: AGPL-3.0-or-later for this repository. API specifications are Apache-2.0 and will be published separately by the `partner-docs` repository (#184), so do not place OpenAPI specs here as the canonical copy. `openapi/openapi.json` is a generated copy for review and release assets, not the canonical copy (see API).
- Before proposing a feature, ask whether it makes the standard more accurate, more trusted, or easier to adopt. If not, it probably belongs in a partner's product.

## Git exception

This section overrides a contributor's global no-commit rule for the `/work-issue` workflow only.

- The workflow may commit (`git commit -s`, with explicit paths) and push, but only on the `issue-<N>-*` branch it creates or resumes.
- After a rebase it may push that branch with `--force-with-lease`. It never force-pushes any other branch.
- It may open a pull request against `main`, edit its description, label it (creating the ready label if it does not exist), and reply to review comments on it.
- It never commits or pushes to `main`, never merges, and never enables auto-merge. A human reviews and merges every pull request.

## Writing style

Docs in this project use plain, short sentences. No emojis and no em dashes. Prefer "player", "match", and "organizer" over generic words like "user" or "entity" (see `CONTEXT.md`).
