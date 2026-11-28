# OPEN backend

[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/openpickleball/backend/badge)](https://scorecard.dev/viewer/?uri=github.com/openpickleball/backend)

Backend services and APIs for [OPEN](https://github.com/openpickleball), the Open Pickleball Evaluation Network.

> **Project status:** OPEN is in early development. This repository holds the project scaffold, the account model with sign-up (behind an age screen: players must be 13 or older), login, email verification, password reset, two-factor authentication with TOTP, recovery codes, and passkeys, login rate limits, a breached password check, session lifetimes, a list of a player's sessions, and new sign-in emails (django-allauth, headless, at `/_allauth/`), the append-only audit log, the OPEN Code scheme for public identifiers, the Player model with its privacy settings, the Club and Organizer models with organizer verification and players' reports of an Organizer to moderators, the email outbox that sends account email, dispute and hold notices, and notices of matches an organizer recorded from the Celery `email` queue (Resend through django-anymail), partner OAuth applications (django-oauth-toolkit) with token and revoke endpoints at `/oauth/token/` and `/oauth/revoke_token/`, the Match, Match Participant, Game, Match Flag, and Match Confirmation models of the match ledger with score validation, duplicate detection, match submission permissions, and the confirmation workflow (confirm, dispute, void, correct, and expire), the rating engine package, pinned to its `v1.0.0` tag (see `docs/engine.md`), the rating state, rating history, and replay models that store the engine's ratings with each replay's provenance, the API mount at `/api/v1/`, the Celery background task setup, static file serving and CORS for the API, a gunicorn configuration, the DigitalOcean App Platform spec for production (`.do/production.yaml`), and the local development services so far. The API's operations are the liveness and readiness checks, at `/api/v1/health/live` and `/api/v1/health/ready`, the signed-in Account at `/api/v1/accounts/me` (read it, or delete it and its personal data), a player's own match submission at `/api/v1/matches/`, one match for its players at `/api/v1/matches/{code}`, confirm and dispute at `/api/v1/matches/{code}/confirm` and `/dispute`, an Event organizer's settlement of a dispute at `/api/v1/matches/{code}/resolve-dispute` and `/dispute-correction`, with the open disputes a member can settle at `/api/v1/me/matches/organizer-disputes`, a player's own disputed matches at `/api/v1/me/matches/disputes`, a vouch that a held possible duplicate is not one at `/api/v1/matches/{code}/vouch`, with the holds the signed-in Account may vouch for at `/api/v1/me/matches/holds`, the matches waiting for the signed-in player's confirmation at `/api/v1/me/matches/awaiting-confirmation`, a player's profile at `/api/v1/players/{code}`, a player's match history at `/api/v1/players/{code}/matches`, player search by display name at `/api/v1/players/search`, a player's current ratings at `/api/v1/players/{code}/ratings` and rating history at `/api/v1/players/{code}/rating-history`, both labeled as demonstration ratings, the signed-in Account's own Player at `/api/v1/me/player` (create, read, and change it), a copy of the signed-in Account's data at `/api/v1/me/export` (requested after a reauthentication, then emailed as a single-use link) and its download at `/api/v1/me/export/download`, an organizer's Events at `/api/v1/organizer/events` (list, create, change, and delete them), an organizer's CSV results upload at `/api/v1/organizer/events/{code}/results-files` with its processing report at `/api/v1/organizer/results-files/{code}`, the signed-in Account's own Organizers at `/api/v1/organizer/organizers` (list them, or create one), an Organizer admin's self-service at `/api/v1/organizer/organizers/{code}` (rename, invite members by email, list members and change or remove them, and ask moderators to verify it) with invite acceptance at `/api/v1/organizer/invites/accept` and the Clubs that let it name them at `/api/v1/organizer/organizers/{code}/club-grants`, Club self-service at `/api/v1/clubs` (create a Club) and `/api/v1/clubs/{code}` (read it, or as its admin change it, invite and remove admins, and grant Organizers the right to name it) with invite acceptance at `/api/v1/clubs/invites/accept`, and a player's report of an Organizer to moderators at `/api/v1/organizers/{code}/reports`. No OPEN rating is live.

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

The `.env` copy gives every command the settings it needs, and its values point at the local services. Before you run the app, set `FIELD_ENCRYPTION_KEYS` in `.env` to a key of your own (see [Settings](#settings)); the tests generate their own. `uv sync` installs the dependencies from `uv.lock`. `docker compose up -d --wait` starts Postgres and Valkey and returns once both are healthy.

To run the app, start the development server:

```bash
uv run python manage.py runserver
```

The interactive API docs are then at http://127.0.0.1:8000/api/v1/docs, and the OpenAPI document is at http://127.0.0.1:8000/api/v1/openapi.json. For a quick check, http://127.0.0.1:8000/api/v1/health/live answers when the app is up. The account endpoints (sign-up, login, email verification, password reset, two-factor authentication, sessions) are documented at http://127.0.0.1:8000/_allauth/openapi.html. With `DJANGO_DEBUG` on, their emails print to the Celery worker's console.

### Settings

Settings come from environment variables. For local development, copy `.env.example` to `.env`; values already set in your shell win over the file. To run Django's system checks, which need no database:

```bash
uv run python manage.py check
```

Without a `.env` or the variables in your environment, every command that loads settings stops with an error. Django's commands name the missing variable; mypy does not (see below). `DJANGO_DEBUG` is off unless set to a true value, and with it off `DJANGO_SECRET_KEY` is required. `FRONTEND_URL` is required too: it is the web app's origin, which links in account emails open, and it must be https when `DJANGO_DEBUG` is off. If your `.env` predates it, copy the line from `.env.example`.

`FIELD_ENCRYPTION_KEYS` holds the keys that encrypt a Player's legal name, birth year and month, and gender division, every MFA secret (ADR 0046), and each player data export file (ADR 0051): Fernet keys, comma separated, newest first. With `DJANGO_DEBUG` off it is required. With it on it may be empty, but then reading or writing a Player fails, and so does building a data export, so set one for local work. Generate a key with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Never commit a key, and never reuse a local key in production. Losing every key loses the encrypted data, so keep production's keys backed up apart from the database. To rotate, put the new key first, keep the old one after it, run `uv run python manage.py reencrypt_fields`, then remove the old key.

Docker Compose also reads `.env` and treats a `$` in a value as a variable reference. If your secret key contains `$`, wrap it in single quotes: `DJANGO_SECRET_KEY='...'`.

### Local services

Migrations and tests need the Postgres that `DATABASE_URL` points to. In pooled mode (see [Connection pool](#connection-pool)), `migrate` uses the one `DIRECT_DATABASE_URL` points to. `compose.yaml` runs Postgres and Valkey for local development, and the values in `.env.example` point at them. Docker must be running.

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

To serve the app the way production does, with `DJANGO_DEBUG` off, collect the static files first and then start gunicorn. gunicorn reads `gunicorn.conf.py`, which takes `PORT` (default 8000) and `WEB_CONCURRENCY` (default 2 workers) from the process environment, not from `.env`. With `DJANGO_DEBUG` off, settings redirect plain http to https, so a local request needs the `X-Forwarded-Proto: https` header that the hosting proxy would set, and `DJANGO_ALLOWED_HOSTS` must name the host you request (for example `127.0.0.1`). The two health checks answer a plain http request with any host (see [App Platform](#app-platform)). `FRONTEND_URL` must be https, for example `https://app.example.org`, `ADMIN_URL` must name the admin's private path, for example `staff-x7k2/` (never `admin/`, the local default), and `FIELD_ENCRYPTION_KEYS` must hold at least one key (see [Settings](#settings)).

```bash
uv run python manage.py collectstatic --noinput
uv run gunicorn
```

With `DJANGO_DEBUG` off, the admin and the API docs pages (`/api/v1/docs` and `/_allauth/openapi.html`) return an error until `collectstatic` has run. This also applies to `runserver` and the compose `web` profile.

The admin lets in only a staff Account with two-factor authentication by an authenticator app (TOTP, ADR 0043; a passkey alone does not count there, ADR 0056), one made with `createsuperuser` included. Set it up first through the account endpoints: sign in, verify the address, reauthenticate (`auth/reauthenticate`), then add TOTP at `/_allauth/{client}/v1/account/authenticators/totp`. Until then the admin shows only a page asking for it, and the API answers its requests with 403 `mfa_required`. The admin's login page (at `ADMIN_URL`, `admin/` locally) asks for the password and then a code from the authenticator app; it signs in through allauth's browser endpoints. A session opens the admin for `ADMIN_SESSION_SECONDS` (default an hour) after its latest code, and a change needs a code from the last `ADMIN_REAUTHENTICATION_SECONDS` (default five minutes): otherwise the login page asks for a new code.

With `DJANGO_DEBUG` off, the session and CSRF cookies are named `__Host-sessionid` and `__Host-csrftoken` (with it on, `sessionid` and `csrftoken`), so a web app that reads the CSRF token must use the name for its mode. Every response carries a Content Security Policy that allows scripts, styles, and requests from the same origin only (ADR 0040). It is enforced with `DJANGO_DEBUG` off and report-only with it on; set `CSP_REPORT_ONLY` to `true` or `false` to choose. A request that declares a body over 2.5 MiB gets a 413 before it is read, except a CSV results upload, which has its own limit.

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

### Connection pool

Production can reach Postgres through a PgBouncer connection pool in transaction mode, such as a DigitalOcean connection pool (ADR 0038). Then `DATABASE_URL` is the pool's connection string and `DIRECT_DATABASE_URL` is the database's own. A DigitalOcean pool's connection string names the pool as its database, not the database itself, so the two URLs differ in more than the port. With `DIRECT_DATABASE_URL` set:

- Requests, Celery tasks, and beat use the pool. A request or task closes its connection when it ends. Beat keeps one connection open, which PgBouncer serves like any other client.
- `manage.py migrate` from the command line with no `--database` points the default connection at the direct database for its own process, so every migration, data migrations included, runs on the direct connection. The image's `python manage.py migrate` does this too. An explicit `--database` is honored as given (`--database default` goes through the pool), and so is an in-process `call_command("migrate")`, which never rebinds.
- Settings refuse `prepare_threshold`, `assume_role`, and `pool` in the `DATABASE_URL` query, because a pool in transaction mode cannot serve them.

Leave `DIRECT_DATABASE_URL` empty when `DATABASE_URL` is the database itself, as in local development and CI. These protections (closed connections, no server-side cursors, refused URL options) apply only when `DIRECT_DATABASE_URL` is set, so a pool URL in `DATABASE_URL` without it runs unprotected.

Size the pool for client connections at their busiest. Each gunicorn worker (`WEB_CONCURRENCY`, sync workers) holds at most one connection per instance, each Celery worker child process holds one (the worker's `--concurrency`, which defaults to the number of CPUs), and beat holds one. Multiply by the number of instances. PgBouncer accepts many more client connections than its pool size and queues clients while every server connection is busy. A transaction that waits for a lock keeps its server connection for the whole wait, so lock waits use up the pool too. Migrations use direct connections, outside the pool, and these count against the cluster's own connection limit.

To try the pool locally, start PgBouncer in transaction mode on 127.0.0.1:6432, with prepared statements off and a small pool. Its config is inline in `compose.yaml` (a `configs` block with `content`), which needs Docker Compose 2.23.1 or later:

```bash
docker compose --profile pgbouncer up -d --wait
```

Then set `DATABASE_URL=postgres://open@localhost:6432/open` and `DIRECT_DATABASE_URL=postgres://open@localhost:5432/open`. PgBouncer routes every database name to the compose Postgres, so the tests run through it too. At the end of such a run, the test databases cannot be dropped, because PgBouncer still holds idle server connections to them: pytest warns and leaves them behind. Drop them from Postgres afterwards with `DROP DATABASE ... WITH (FORCE)`, or restart PgBouncer before the next run.

With `DJANGO_DEBUG` off, settings trust the `X-Forwarded-Proto` header to tell https from http. So run the web container only behind the hosting proxy, which sets that header, and never let clients reach it directly. To test it locally, send `X-Forwarded-Proto: https` with each request, as in [Serving like production](#serving-like-production).

### App Platform

Production runs on DigitalOcean App Platform, from one app spec: `.do/production.yaml` (app `open-backend-production`). There is no staging environment, to keep costs down: changes are tried in local development (the compose services, and the production image as in [Serving like production](#serving-like-production)), then deployed to production. The spec has:

- `web`: the image's gunicorn on port 8000. Its readiness check is `/api/v1/health/ready`, which stops traffic while the database or cache is down. A database connect gives up after 5 seconds, but a query is not bounded: if the pool answers and Postgres behind it hangs, the check can wait past gunicorn's worker timeout (ADR 0042; bounding it is left to #66). Its liveness check is `/api/v1/health/live`, which restarts the container only when the process stops answering.
- `worker`: `celery -A config worker -Q ratings,email,webhooks,default -l info --concurrency 2`, every queue.
- `beat`: `celery -A config beat` with the database scheduler, exactly one instance. During a deploy App Platform may run the old and the new beat together for a short time, so a periodic task can be sent twice. Every task must already be safe to run twice (ADR 0006).
- `migrate`: a pre-deploy job, `python manage.py migrate`. It runs before each deploy goes live and migrates on the direct connection by itself (ADR 0038). A failed migration stops the deploy. The release that encrypts personal data (ADR 0046) must deploy with no traffic to the old release, before production opens to players or with the app stopped while `migrate` runs: the old release keeps serving during the pre-deploy job, and after the data migrations it would read tokens as plain values and write plain-text Players and TOTP secrets that the new release cannot read.
- `pg`: a managed Postgres cluster (`open-production-pg`), database `open`, user `open`.
- `valkey`: a managed Valkey cluster (`open-production-valkey`), the cache and Celery broker (ADR 0006). DigitalOcean's Valkey takes TLS only (`rediss://`).

Every component runs the same image on the smallest instance size, `apps-s-1vcpu-0.5gb`. The variables are set once, at app level, so every component gets them: `DATABASE_URL` is the pool `open-pool` (`${pg.open-pool.DATABASE_URL}`), `DIRECT_DATABASE_URL` the database itself (`${pg.DATABASE_URL}`), `REDIS_URL` the Valkey cluster (`${valkey.DATABASE_URL}`), and `DJANGO_ALLOWED_HOSTS` the app's domain (`${APP_DOMAIN}`). `FRONTEND_URL` is `https://app.openpb.org` (#182). Until the web app answers there (#183), the links in verification and reset emails open a page that does not exist yet. `CLIENT_IP_HEADER` stays at its default, `do-connecting-ip`.

The spec holds no secret values. `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` (see [Settings](#settings)), `RESEND_API_KEY`, and `ADMIN_URL` (the admin's private path, such as `staff-x7k2/`, never `admin/`) are declared as secrets with no value. Set them in the App Platform console, or in a spec exported from the running app, where App Platform shows them encrypted (`EV[...]`). Never commit a value.

App Platform's HTTP health probes reach the container directly, over plain http, with a Host header that `DJANGO_ALLOWED_HOSTS` refuses. `HealthProbeMiddleware` (`config/health_probe.py`, ADR 0042) answers a GET of exactly the two health paths in that case. Every other request keeps the host check and the https redirect.

#### Before the first deploy

The spec does not set these. #68 and #63 do:

- Create the Postgres and Valkey clusters with the cluster names above, in one region, and set the app's `region` to match. The spec leaves `region` unset.
- Create the pool `open-pool` in transaction mode on database `open` for user `open`: `doctl databases pool create <cluster-id> open-pool --size 10 --mode transaction --db open --user open`.
- Set Valkey's eviction policy to `volatile-lru`: `doctl databases configuration update <cluster-id> --engine valkey --config-json '{"valkey_maxmemory_policy":"volatile-lru"}'`. Valkey holds the cache and the Celery queues. Cache and throttle keys all expire, and queued tasks never do, so `volatile-lru` evicts only cache keys. An `allkeys-*` policy could drop queued tasks, and `noeviction` would make every cache write fail once Valkey is full.
- Make user `open` the owner of database `open`: as `doadmin`, run `ALTER DATABASE open OWNER TO open;`. On Postgres 15 and later the `public` schema belongs to the database owner, so a user that does not own the database cannot create tables, and the first migrate fails. Today both URLs use `open`, so the runtime user owns every table, the audit table included. #103 decides separate migration and runtime users, as the Audit log rules require.
- Limit each cluster's trusted sources to the app (#68, #103).
- Set the four secret values: `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` (a newly generated Fernet key, backed up apart from the database), `RESEND_API_KEY`, and `ADMIN_URL` (a path only staff know, letters, digits, hyphens, and underscores ending in `/`).
- Give App Platform access to the image on GHCR, and deploy by digest instead of the `main` tag (#63).
- On the first deploy, confirm that `${valkey.DATABASE_URL}` is Valkey's `rediss://` connection string. DigitalOcean documents `DATABASE_URL` for every database and `REDIS_URL` only for its older Redis engine.

#### Pool sizing

One instance each of web, worker, and beat opens at most 5 client connections: web 2 (`WEB_CONCURRENCY`, default 2), worker 2 (`--concurrency 2`), and beat 1. The smallest cluster (1 GiB) allows 25 connections, 3 of them reserved, so 22 are usable. DigitalOcean suggests a pool of about half the usable connections, so `open-pool` has size 10. That leaves room for a second web instance and for the old and new instances running side by side during a deploy. Migrations use one direct connection, outside the pool.

#### Validate the spec

```bash
doctl apps spec validate --schema-only .do/production.yaml
```

`--schema-only` checks the fields locally. A full `doctl apps spec validate`, which also checks the bindings and the instance size, needs an authenticated doctl.

#### Check the client IP header

The per-IP throttle reads the client IP from `do-connecting-ip` (ADR 0030). Until this check passes on production, no public operation goes live: a client that can set that header itself could spread its requests across any number of throttle buckets.

1. On the production `web` component, before it opens to players (#68), set `GUNICORN_CMD_ARGS` to `--access-logfile - --access-logformat "%({http_do_connecting_ip}e)s %(s)s"` and redeploy. gunicorn applies `GUNICORN_CMD_ARGS` after `gunicorn.conf.py`. The format logs the header's value as Django reads it (gunicorn joins repeated headers with a comma) and the status, never the request line, which can hold an email verification or password reset key.
2. From a machine whose public IP you know, run `curl -sS -o /dev/null -H 'do-connecting-ip: 203.0.113.7' https://<app domain>/api/v1/health/live`.
3. Read the web runtime logs with `doctl apps logs <app-id> web --type run`. The check passes only if the logged value is exactly your real IP. A comma-separated list, or `203.0.113.7`, fails it.
4. Remove `GUNICORN_CMD_ARGS` and redeploy, since the line logs client IPs.
5. Record the result on #61 and #34. If the check fails, no public operation goes live until the setting or the throttle changes.

#### Monthly cost

Prices from DigitalOcean's pricing pages, checked 2026-11-27:

| Component | Size | Per month |
| --- | --- | --- |
| web | `apps-s-1vcpu-0.5gb` (512 MiB, shared CPU) | $5.00 |
| worker | `apps-s-1vcpu-0.5gb` | $5.00 |
| beat | `apps-s-1vcpu-0.5gb` | $5.00 |
| migrate job | `apps-s-1vcpu-0.5gb`, billed per second while it runs | under $0.01 per deploy |
| Postgres | 1 GiB RAM, 1 vCPU, single node, from 10 GiB storage | $15.15 |
| Valkey | 1 GiB | $15.00 |
| Total | | $45.15 |

There is no staging environment, so this is the whole hosting cost. Each $5 container includes 50 GiB of outbound transfer, pooled across the app, and more costs $0.02 per GiB. Running beat inside the worker (`celery worker -B`) would save $5 a month, but decision Q16 in #1 keeps beat a separate process.

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

CI runs lint, types, and tests on every pull request. The test job also runs `manage.py check` and the missing-migrations check (`makemigrations --check --dry-run`), and fails when the committed OpenAPI document `openapi/openapi.json` is out of date (`export_openapi_schema --check`). After changing the API, regenerate it with `uv run python manage.py export_openapi_schema` and commit it. The audit workflow checks the locked dependencies for known vulnerabilities on every pull request and weekly; see [Supply chain checks](CONTRIBUTING.md#supply-chain-checks).

### Email

Requests never send email, the data export's link email and the Organizer invite email included. Each message is stored in the email outbox and sent by a Celery task on the `email` queue after the transaction commits (ADR 0016), so run a worker to see it. With `DJANGO_DEBUG` on and `EMAIL_BACKEND` empty, the worker prints every message to its console. With `DJANGO_DEBUG` off, `EMAIL_BACKEND` and `DEFAULT_FROM_EMAIL` are required, and backends that print or keep messages are refused.

Production sends through Resend with django-anymail: set `EMAIL_BACKEND=anymail.backends.resend.EmailBackend` and `RESEND_API_KEY`. The sending domain, its DNS records (SPF, DKIM, DMARC), and the DMARC rollout are in `docs/email.md`. To switch to Amazon SES, run `uv add "django-anymail[amazon-ses]"`, set `EMAIL_BACKEND=anymail.backends.amazon_ses.EmailBackend`, and set `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` in the environment. No code changes. The SES backend uses boto3, which ignores `ANYMAIL_REQUESTS_TIMEOUT`: without more, a slow SES request reaches the send task's 60-second soft time limit, and the sweep retries it about an hour later instead of a backoff retry. For backoff retries, you can optionally add `ANYMAIL["AMAZON_SES_CLIENT_PARAMS"] = {"config": Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 1})}` in settings, with `Config` from `botocore.config`.

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
