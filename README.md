# OPEN backend

Backend services and APIs for [OPEN](https://github.com/openpickleball), the Open Pickleball Evaluation Network.

> **Project status:** OPEN is in early development. This repository holds the project scaffold, the account model, and the local development services so far, and no OPEN rating is live.

## Technology

Python 3.13 and Django 5.2 LTS, managed with [uv](https://docs.astral.sh/uv/). The stack decisions are in [issue #1](https://github.com/openpickleball/backend/issues/1).

## Setup and tests

Settings come from environment variables. For local development, copy `.env.example` to `.env`; values already set in your shell win over the file. Then install the dependencies from `uv.lock` and run Django's system checks:

```bash
cp .env.example .env
uv sync
uv run python manage.py check
```

Without a `.env` or the variables in your environment, every command that loads settings stops with an error naming the missing variable. `DJANGO_DEBUG` is off unless set to a true value, and with it off `DJANGO_SECRET_KEY` is required.

Migrations and tests need the Postgres that `DATABASE_URL` points to. `compose.yaml` runs Postgres and Valkey for local development, and the values in `.env.example` point at them. With Docker running:

```bash
docker compose up -d --wait
uv run python manage.py migrate
uv run python manage.py test apps config
uv run python manage.py runserver
```

`--wait` returns once both services are healthy. The services listen on `127.0.0.1` only: Postgres on port 5432 and Valkey on 6379. Stop any other Postgres, Redis, or Valkey on those ports first. `docker compose down` stops them and keeps the data; `docker compose down -v` also deletes it. To run the app in a container on http://127.0.0.1:8000 instead of with `runserver` on the host, use `docker compose --profile web up -d --wait`. It uses the same port as `runserver`, so stop one before starting the other.

Docker Compose also reads `.env` and treats a `$` in a value as a variable reference. If your secret key contains `$`, wrap it in single quotes: `DJANGO_SECRET_KEY='...'`.

No email is sent yet. With `DJANGO_DEBUG` on, every message prints to the console. With it off, messages are discarded until #35 adds a real email provider.

Full setup and test instructions will be added as the foundation work lands.

## Test data

Use only made-up or properly de-identified data. Do not put real player names, contact details, or match histories in this repository, including in test fixtures and screenshots.

## Contributing

See the [contributing guide](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md). Every commit must be signed off. Please report security problems privately, as described in the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md).

## License

This repository is licensed under [AGPL-3.0-or-later](LICENSE). See [how OPEN is licensed](https://github.com/openpickleball/.github/blob/main/LICENSING.md). API specifications are licensed under Apache-2.0 and will be published separately.
