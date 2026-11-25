# OPEN backend

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/openpickleball/backend/badge)](https://scorecard.dev/viewer/?uri=github.com/openpickleball/backend)

Backend services and APIs for [OPEN](https://github.com/openpickleball), the Open Pickleball Evaluation Network.

> **Project status:** OPEN is in early development. This repository holds the project scaffold, the account model with sign-up (behind an age screen: players must be 13 or older), login, email verification, and password reset (django-allauth, headless, at `/_allauth/`), the append-only audit log, the OPEN Code scheme for public identifiers, the Player model with its privacy settings, the Club and Organizer models with organizer verification, the email outbox that sends account email from the Celery `email` queue (Resend through django-anymail), partner OAuth applications (django-oauth-toolkit) with token and revoke endpoints at `/oauth/token/` and `/oauth/revoke_token/`, the Match, Match Participant, Game, Match Flag, and Match Confirmation models of the match ledger with score validation, duplicate detection, match submission permissions, and the confirmation workflow (confirm, dispute, void, correct, and expire), the rating engine package, pinned to its `v1.0.0` tag (see `docs/engine.md`), the rating state, rating history, and replay models that store the engine's ratings with each replay's provenance, the API mount at `/api/v1/`, the Celery background task setup, static file serving and CORS for the API, a gunicorn configuration, and the local development services so far. The API's operations are the liveness and readiness checks, at `/api/v1/health/live` and `/api/v1/health/ready`, the signed-in Account at `/api/v1/accounts/me`, a player's own match submission at `/api/v1/matches/`, one match for its players at `/api/v1/matches/{code}`, confirm and dispute at `/api/v1/matches/{code}/confirm` and `/dispute`, an Event organizer's settlement of a dispute at `/api/v1/matches/{code}/resolve-dispute` and `/dispute-correction`, with its open disputes at `/api/v1/me/matches/organizer-disputes`, a vouch that a held possible duplicate is not one at `/api/v1/matches/{code}/vouch`, with the holds the signed-in Account may vouch for at `/api/v1/me/matches/holds`, the matches waiting for the signed-in player's confirmation at `/api/v1/me/matches/awaiting-confirmation`, a player's profile at `/api/v1/players/{code}`, a player's match history at `/api/v1/players/{code}/matches`, player search by display name at `/api/v1/players/search`, a player's current ratings at `/api/v1/players/{code}/ratings` and rating history at `/api/v1/players/{code}/rating-history`, both labeled as demonstration ratings, the signed-in Account's own Player at `/api/v1/me/player` (create, read, and change it), an organizer's Events at `/api/v1/organizer/events` (list, create, change, and delete them), and an organizer's CSV results upload at `/api/v1/organizer/events/{code}/results-files` with its report at `/api/v1/organizer/results-files/{code}`. No OPEN rating is live.

## Technology

Python 3.13 and Django 5.2 LTS, managed with [uv](https://docs.astral.sh/uv/). The stack decisions are in [issue #1](https://github.com/openpickleball/backend/issues/1). [ADR 0001](docs/adr/0001-django.md) explains why the backend uses Django, and [`docs/adr/`](docs/adr/) holds the other architecture decision records.

## Setup and tests

### Prerequisites

- [git](https://git-scm.com/).
- [uv](https://docs.astral.sh/uv/getting-started/installation/). The project uses Python 3.13 (see `.python-version`). If it is missing, uv installs it for you.
- [Docker](https://docs.docker.com/get-docker/) with Compose v2, so that `docker compose` works.
- Ports 5432 and 6379 free on `127.0.0.1`. The local Postgres and Valkey listen there.

### Quick start

From a fresh clone to passing tests:

```bash
git clone https://github.com/openpickleball/backend.git
cd backend
cp .env.example .env
uv sync
docker compose up -d --wait
uv run python manage.py migrate
uv run pytest
```

The `.env` copy gives every command the settings it needs, and its values point at the local services. `uv sync` installs the dependencies from `uv.lock`. `docker compose up -d --wait` starts Postgres and Valkey and returns once both are healthy.

To run the app, start the development server:

```bash
uv run python manage.py runserver
```

The interactive API docs are then at http://127.0.0.1:8000/api/v1/docs, and the OpenAPI document is at http://127.0.0.1:8000/api/v1/openapi.json. For a quick check, http://127.0.0.1:8000/api/v1/health/live answers when the app is up. The account endpoints (sign-up, login, email verification, password reset) are documented at http://127.0.0.1:8000/_allauth/openapi.html. With `DJANGO_DEBUG` on, their emails print to the Celery worker's console.

### Settings

Settings come from environment variables. For local development, copy `.env.example` to `.env`; values already set in your shell win over the file. To run Django's system checks, which need no database:

```bash
uv run python manage.py check
```

Without a `.env` or the variables in your environment, every command that loads settings stops with an error. Django's commands name the missing variable; mypy does not (see below). `DJANGO_DEBUG` is off unless set to a true value, and with it off `DJANGO_SECRET_KEY` is required. `FRONTEND_URL` is required too: it is the web app's origin, which links in account emails open, and it must be https when `DJANGO_DEBUG` is off. If your `.env` predates it, copy the line from `.env.example`.

Docker Compose also reads `.env` and treats a `$` in a value as a variable reference. If your secret key contains `$`, wrap it in single quotes: `DJANGO_SECRET_KEY='...'`.

### Local services

Migrations and tests need the Postgres that `DATABASE_URL` points to. `compose.yaml` runs Postgres and Valkey for local development, and the values in `.env.example` point at them. Docker must be running.

`--wait` returns once both services are healthy. The services listen on `127.0.0.1` only: Postgres on port 5432 and Valkey on 6379. Stop any other Postgres, Redis, or Valkey on those ports first. `docker compose down` stops them and keeps the data; `docker compose down -v` also deletes it. To run the app in a container on http://127.0.0.1:8000 instead of with `runserver` on the host, use `docker compose --profile web up -d --wait`. It uses the same port as `runserver`, so stop one before starting the other. The same profile also starts a Celery worker and beat. Beat waits until the migrations are applied, so run `uv run python manage.py migrate` first.

### Tests

`uv run pytest` reports coverage and fails if it drops below the floor set in `pyproject.toml`. That floor counts all of `apps` and `config`, so when you run only some tests (one file, or `-k`), add `--no-cov`. Add `-n auto` to run the tests in parallel, one worker per CPU, as CI does. Each worker creates its own test database.

### Background tasks

Background tasks run in Celery workers, with Valkey as the broker at the same `REDIS_URL` as the cache. To run a worker and the periodic task scheduler on the host:

```bash
uv run celery -A config worker -Q ratings,email,webhooks,default -l info
uv run celery -A config beat -l info
```

Run only one beat process. Tests run tasks eagerly, so they need no worker and no broker.

### Serving like production

To serve the app the way production does, with `DJANGO_DEBUG` off, collect the static files first and then start gunicorn. gunicorn reads `gunicorn.conf.py`, which takes `PORT` (default 8000) and `WEB_CONCURRENCY` (default 2 workers) from the process environment, not from `.env`. With `DJANGO_DEBUG` off, settings redirect plain http to https, so a local request needs the `X-Forwarded-Proto: https` header that the hosting proxy would set, and `DJANGO_ALLOWED_HOSTS` must name the host you request (for example `127.0.0.1`). `FRONTEND_URL` must be https, for example `https://app.example.org`.

```bash
uv run python manage.py collectstatic --noinput
uv run gunicorn
```

With `DJANGO_DEBUG` off, the admin and the API docs page return an error until `collectstatic` has run. This also applies to `runserver` and the compose `web` profile.

### Production image

The `Dockerfile` builds the image production runs. It installs only the runtime packages from `uv.lock`, collects the static files at build time, and runs as a non-root user. The image has no uv, git, tests, or dev packages. `.dockerignore` lets only the app's files into the build, so a `.env`, key, or local database in your checkout never reaches the image. Pass every setting as an environment variable when you run it.

```bash
docker build -t open-backend .
```

The default command is gunicorn, which reads `gunicorn.conf.py`, so `PORT` and `WEB_CONCURRENCY` come from the environment. The same image runs migrations, the Celery worker, and beat when you give it another command:

```bash
docker run --rm --env-file prod.env open-backend python manage.py migrate
docker run --rm --env-file prod.env -e PORT=8000 -p 8000:8000 open-backend
docker run --rm --env-file prod.env open-backend celery -A config worker -Q ratings,email,webhooks,default -l info
docker run --rm --env-file prod.env open-backend celery -A config beat -l info
```

Here `prod.env` is a file of settings you keep outside the repository. Run one beat process only.

With `DJANGO_DEBUG` off, settings trust the `X-Forwarded-Proto` header to tell https from http. So run the web container only behind the hosting proxy, which sets that header, and never let clients reach it directly. To test it locally, send `X-Forwarded-Proto: https` with each request, as in [Serving like production](#serving-like-production).

### CORS

A browser page on another origin can read API responses only if its origin is listed in `CORS_ALLOWED_ORIGINS`, comma-separated, as exact origins such as `https://app.example.org` or `http://localhost:3000`. It is empty by default, so no other origin is allowed. Only `/api/` and `/_allauth/` paths send CORS headers. CORS limits what browsers let a page read; it is not access control, and it does not limit other clients.

### Lint, types, and git hooks

Formatting, lint, and type checks use ruff and mypy (strict, with django-stubs). Install the git hooks once, then run every check by hand when you like:

```bash
uv run pre-commit install
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pre-commit run --all-files
```

mypy loads the Django settings, so it needs `.env` (or the variables) like `check` does. Without them mypy fails with "Error constructing plugin instance", and the message does not name the missing variable. The mypy hook checks the whole project, so an untracked `.py` file with type errors also blocks a commit. The ruff and mypy hooks run through `uv run --locked`: after changing dependencies in `pyproject.toml`, run `uv lock` before you commit.

CI runs lint, types, and tests on every pull request. The test job also runs `manage.py check` and the missing-migrations check (`makemigrations --check --dry-run`), and fails when the committed OpenAPI document `openapi/openapi.json` is out of date (`export_openapi_schema --check`). After changing the API, regenerate it with `uv run python manage.py export_openapi_schema` and commit it.

### Email

Requests never send email. Each message is stored in the email outbox and sent by a Celery task on the `email` queue after the transaction commits (ADR 0016), so run a worker to see it. With `DJANGO_DEBUG` on and `EMAIL_BACKEND` empty, the worker prints every message to its console. With `DJANGO_DEBUG` off, `EMAIL_BACKEND` and `DEFAULT_FROM_EMAIL` are required, and backends that print or keep messages are refused.

Staging and production send through Resend with django-anymail: set `EMAIL_BACKEND=anymail.backends.resend.EmailBackend` and `RESEND_API_KEY`. The sending domain, its DNS records (SPF, DKIM, DMARC), and the DMARC rollout are in `docs/email.md`. To switch to Amazon SES, run `uv add "django-anymail[amazon-ses]"`, set `EMAIL_BACKEND=anymail.backends.amazon_ses.EmailBackend`, and set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` in the environment. No code changes. The SES backend uses boto3, which ignores `ANYMAIL_REQUESTS_TIMEOUT`: without more, a slow SES request reaches the send task's 60-second soft time limit, and the sweep retries it about an hour later instead of a backoff retry. For backoff retries, you can optionally add `ANYMAIL["AMAZON_SES_CLIENT_PARAMS"] = {"config": Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})}` in settings, with `Config` from `botocore.config`.

## Test data

Use only made-up or properly de-identified data. Do not put real player names, contact details, or match histories in this repository, including in test fixtures and screenshots. To build test records, prefer the factory_boy factories, such as `UserFactory` in `apps/accounts/tests/factories.py`, which fill in made-up Faker data on example domains.

### Seed a development database

To fill a local database with made-up records, run the seed command with `DJANGO_DEBUG` on:

```bash
uv run python manage.py migrate
uv run python manage.py seed_dev_data --players 20
```

It adds two Clubs, two verified Organizers with one Event each, the Players you ask for (`--players`, default 20, at least 4), and twice as many confirmed singles and doubles Matches. Every name comes from Faker, and every email is on a reserved example domain. Each run adds a new set of records alongside the old ones. The command refuses to run with `DJANGO_DEBUG` off, and it needs the development dependencies that `uv sync` installs.

The audit log is append-only, so `flush` cannot empty a seeded database. To start over, run `docker compose down -v`, then `docker compose up -d --wait` and `migrate`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for this repository's workflow: branches and pull requests, code style, tests, migrations, and the API schema. It adds to the organization [contributing guide](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md). Every commit must be signed off. Please report security problems privately, as described in the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md).

## License

This repository is licensed under [AGPL-3.0-or-later](LICENSE). See [how OPEN is licensed](https://github.com/openpickleball/.github/blob/main/LICENSING.md). API specifications are licensed under Apache-2.0 and will be published separately by the `partner-docs` repository. The `openapi/openapi.json` file here is a generated copy for review and release assets, not the canonical one.
