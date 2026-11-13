# OPEN backend

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/openpickleball/backend/badge)](https://scorecard.dev/viewer/?uri=github.com/openpickleball/backend)

Backend services and APIs for [OPEN](https://github.com/openpickleball), the Open Pickleball Evaluation Network.

> **Project status:** OPEN is in early development. This repository holds the project scaffold, the account model, the API mount at `/api/v1/`, the Celery background task setup, static file serving and CORS for the API, a gunicorn configuration, and the local development services so far. The API's only operations are the liveness and readiness checks, at `/api/v1/health/live` and `/api/v1/health/ready`. No OPEN rating is live.

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

The interactive API docs are then at http://127.0.0.1:8000/api/v1/docs, and the OpenAPI document is at http://127.0.0.1:8000/api/v1/openapi.json. For a quick check, http://127.0.0.1:8000/api/v1/health/live answers when the app is up.

### Settings

Settings come from environment variables. For local development, copy `.env.example` to `.env`; values already set in your shell win over the file. To run Django's system checks, which need no database:

```bash
uv run python manage.py check
```

Without a `.env` or the variables in your environment, every command that loads settings stops with an error. Django's commands name the missing variable; mypy does not (see below). `DJANGO_DEBUG` is off unless set to a true value, and with it off `DJANGO_SECRET_KEY` is required.

Docker Compose also reads `.env` and treats a `$` in a value as a variable reference. If your secret key contains `$`, wrap it in single quotes: `DJANGO_SECRET_KEY='...'`.

### Local services

Migrations and tests need the Postgres that `DATABASE_URL` points to. `compose.yaml` runs Postgres and Valkey for local development, and the values in `.env.example` point at them. Docker must be running.

`--wait` returns once both services are healthy. The services listen on `127.0.0.1` only: Postgres on port 5432 and Valkey on 6379. Stop any other Postgres, Redis, or Valkey on those ports first. `docker compose down` stops them and keeps the data; `docker compose down -v` also deletes it. To run the app in a container on http://127.0.0.1:8000 instead of with `runserver` on the host, use `docker compose --profile web up -d --wait`. It uses the same port as `runserver`, so stop one before starting the other. The same profile also starts a Celery worker and beat. Beat waits until the migrations are applied, so run `uv run python manage.py migrate` first.

### Tests

`uv run pytest` reports coverage and fails if it drops below the floor set in `pyproject.toml`. That floor counts all of `apps` and `config`, so when you run only some tests (one file, or `-k`), add `--no-cov`.

### Background tasks

Background tasks run in Celery workers, with Valkey as the broker at the same `REDIS_URL` as the cache. To run a worker and the periodic task scheduler on the host:

```bash
uv run celery -A config worker -Q ratings,email,webhooks,default -l info
uv run celery -A config beat -l info
```

Run only one beat process. Tests run tasks eagerly, so they need no worker and no broker.

### Serving like production

To serve the app the way production does, with `DJANGO_DEBUG` off, collect the static files first and then start gunicorn. gunicorn reads `gunicorn.conf.py`, which takes `PORT` (default 8000) and `WEB_CONCURRENCY` (default 2 workers) from the process environment, not from `.env`. With `DJANGO_DEBUG` off, settings redirect plain http to https, so a local request needs the `X-Forwarded-Proto: https` header that the hosting proxy would set, and `DJANGO_ALLOWED_HOSTS` must name the host you request (for example `127.0.0.1`).

```bash
uv run python manage.py collectstatic --noinput
uv run gunicorn
```

With `DJANGO_DEBUG` off, the admin and the API docs page return an error until `collectstatic` has run. This also applies to `runserver` and the compose `web` profile.

### CORS

A browser page on another origin can read API responses only if its origin is listed in `CORS_ALLOWED_ORIGINS`, comma-separated, as exact origins such as `https://app.example.org` or `http://localhost:3000`. It is empty by default, so no other origin is allowed. Only `/api/` paths send CORS headers. CORS limits what browsers let a page read; it is not access control, and it does not limit other clients.

### Lint, types, and git hooks

Formatting, lint, and type checks use ruff and mypy (strict, with django-stubs). Install the git hooks once, then run every check by hand when you like:

```bash
uv run pre-commit install
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run pre-commit run --all-files
```

mypy loads the Django settings, so it needs `.env` (or the variables) like `check` does. Without them mypy fails with "Error constructing plugin instance", and the message does not name the missing variable. The mypy hook checks the whole project, so an untracked `.py` file with type errors also blocks a commit. The ruff and mypy hooks run through `uv run --locked`: after changing dependencies in `pyproject.toml`, run `uv lock` before you commit.

CI runs lint, types, and tests on every pull request. The test job also runs `manage.py check` and the missing-migrations check (`makemigrations --check --dry-run`).

### Email

No email is sent yet. With `DJANGO_DEBUG` on, every message prints to the console. With it off, messages are discarded until #35 adds a real email provider.

## Test data

Use only made-up or properly de-identified data. Do not put real player names, contact details, or match histories in this repository, including in test fixtures and screenshots. To build test records, prefer the factory_boy factories, such as `UserFactory` in `apps/accounts/tests/factories.py`, which fill in made-up Faker data on example domains.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for this repository's workflow: branches and pull requests, code style, tests, and migrations. It adds to the organization [contributing guide](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md). Every commit must be signed off. Please report security problems privately, as described in the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md).

## License

This repository is licensed under [AGPL-3.0-or-later](LICENSE). See [how OPEN is licensed](https://github.com/openpickleball/.github/blob/main/LICENSING.md). API specifications are licensed under Apache-2.0 and will be published separately.
