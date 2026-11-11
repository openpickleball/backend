# OPEN backend

Backend services and APIs for [OPEN](https://github.com/openpickleball), the Open Pickleball Evaluation Network.

> **Project status:** OPEN is in early development. This repository holds the project scaffold and the account model so far, and no OPEN rating is live.

## Technology

Python 3.13 and Django 5.2 LTS, managed with [uv](https://docs.astral.sh/uv/). The stack decisions are in [issue #1](https://github.com/openpickleball/backend/issues/1).

## Setup and tests

Install the dependencies from `uv.lock` and run Django's system checks:

```bash
uv sync
uv run python manage.py check
```

Migrations and tests need a reachable Postgres on `localhost:5432` with role and database `open`. The role has no password, so the server must allow trust authentication, and the role needs permission to create databases for the test run. Local Postgres setup arrives with docker compose. Until then, with your own Postgres running:

```bash
uv run python manage.py migrate
uv run python manage.py test apps
```

Full setup and test instructions will be added as the foundation work lands.

## Test data

Use only made-up or properly de-identified data. Do not put real player names, contact details, or match histories in this repository, including in test fixtures and screenshots.

## Contributing

See the [contributing guide](https://github.com/openpickleball/.github/blob/main/CONTRIBUTING.md). Every commit must be signed off. Please report security problems privately, as described in the [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md).

## License

This repository is licensed under [AGPL-3.0-or-later](LICENSE). See [how OPEN is licensed](https://github.com/openpickleball/.github/blob/main/LICENSING.md). API specifications are licensed under Apache-2.0 and will be published separately.
