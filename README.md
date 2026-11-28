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

### Environment variables

Every variable that `config/settings.py` and `gunicorn.conf.py` read is in the table below. `config/tests/test_env_var_table.py` fails when settings read a variable the table leaves out, when a row names a variable nothing reads, or when the table and `.do/production.yaml` disagree. Add a row in the same pull request as a new variable.

Where each value is set:

- **Local development:** `.env` (copy `.env.example`, which lists every variable below) or your shell, which wins over the file. `PORT` and `WEB_CONCURRENCY` come from the shell only.
- **Production:** the app-level `envs` in `.do/production.yaml`, every one `RUN_TIME`. In the Production column, `spec` means the spec holds the value, `bound` that App Platform fills it from the spec's database component, `console` that the spec declares it `type: SECRET` with no value and a maintainer sets it in the App Platform console (see [Before the first deploy](#before-the-first-deploy)), `App Platform` that the platform sets it, `default` that production leaves it unset, and `never` that settings refuse to start when it has a value.
- **CI:** the `test` job in `.github/workflows/ci.yml` sets made-up production-like values for its deploy check. They are not secrets.
- **GitHub environments:** none yet. The deploy workflow (#63) adds a `production` GitHub environment with a required reviewer, holding the DigitalOcean token the workflow uses. Settings never read it. #63 lists what it adds here.

No secret is committed. `.env` is ignored by git, `.env.example` holds placeholders only, the spec declares secrets without values, and the image never contains a `.env` (see [Production image](#production-image)). Every example below is a made-up placeholder; secrets show only `<angle brackets>`. In the Required column, `DEBUG off` means settings refuse to start without it when `DJANGO_DEBUG` is off.

| Variable | Purpose | Required | Secret | Example | Production |
| --- | --- | --- | --- | --- | --- |
| `DJANGO_DEBUG` | Debug mode. Off unless a true value. | no | no | `false` | spec |
| `DJANGO_SECRET_KEY` | Django's signing key. With DEBUG on and empty, a development-only key is used. | DEBUG off | yes | `<long random key>` | console |
| `DJANGO_ALLOWED_HOSTS` | Hosts Django answers on, comma-separated. Empty answers none with DEBUG off. | no | no | `api.example.org,app.example.org` | spec |
| `ADMIN_URL` | The admin's private path (ADR 0045). Empty with DEBUG on means `admin/`. | DEBUG off | yes | `<private-path>/` | console |
| `ADMIN_SESSION_SECONDS` | How long after its latest MFA step a staff session opens the admin. Default 3600. | no | no | `3600` | default |
| `ADMIN_REAUTHENTICATION_SECONDS` | How recent the MFA step must be for an admin write. Default 300, or the session if lower. | no | no | `300` | default |
| `FIELD_ENCRYPTION_KEYS` | Fernet keys that encrypt personal data fields, newest first (ADR 0046). | DEBUG off | yes | `<newest Fernet key>,<older Fernet key>` | console |
| `DATABASE_URL` | Postgres, or its connection pool in transaction mode (ADR 0038). | yes | yes | `postgres://open:<password>@<host>:5432/open` | bound |
| `DIRECT_DATABASE_URL` | The database itself when `DATABASE_URL` is a pool; `migrate` uses it. Empty means no pool. | no | yes | `postgres://open:<password>@<host>:5432/open` | bound |
| `REDIS_URL` | Valkey: the cache and the Celery broker (ADR 0006). `rediss://` for TLS. | yes | yes | `rediss://default:<password>@<host>:6379/0` | bound |
| `CELERY_BROKER_URL` | Must stay unset: Celery uses `REDIS_URL`. | no | no | (unset) | never |
| `CELERY_BROKER_READ_URL` | Must stay unset: Celery uses `REDIS_URL`. | no | no | (unset) | never |
| `CELERY_BROKER_WRITE_URL` | Must stay unset: Celery uses `REDIS_URL`. | no | no | (unset) | never |
| `CELERY_RESULT_BACKEND` | Must stay unset: Celery uses `REDIS_URL`. | no | no | (unset) | never |
| `FRONTEND_URL` | The web app's origin, where links in account and match emails open. https with DEBUG off. | yes | no | `https://app.example.org` | spec |
| `CORS_ALLOWED_ORIGINS` | Exact browser origins allowed to call `/api/` and `/_allauth/`, comma-separated. Empty allows none. | no | no | `https://app.example.org` | default |
| `CSRF_TRUSTED_ORIGINS` | Extra origins the CSRF check trusts, comma-separated, https with DEBUG off. Empty trusts none. | no | no | `https://app.example.org` | default |
| `CSP_REPORT_ONLY` | Content Security Policy mode (ADR 0040). Empty means report-only with DEBUG on, enforced with it off. | no | no | `false` | default |
| `WEBAUTHN_RP_ID` | The passkey relying party ID (ADR 0056). Default `openpb.org`. | no | no | `example.org` | default |
| `WEBAUTHN_ALLOWED_ORIGINS` | Exact origins passkeys may be used from, https with DEBUG off. Empty means `FRONTEND_URL`. | no | no | `https://app.example.org` | default |
| `OPEN_PUBLIC_PROFILE_URL` | The base of the rating lookup's `profile_url`. Default `https://app.openpb.org/players/`. | no | no | `https://app.example.org/players/` | default |
| `SECURITY_TXT_CONTACT` | The contact in `/.well-known/security.txt`. Default `mailto:info@openpb.org`. | no | no | `mailto:security@example.org` | default |
| `SECURITY_TXT_CANONICAL` | The canonical URL in `/.well-known/security.txt`. Empty means the request's URL. | no | no | `https://api.example.org/.well-known/security.txt` | default |
| `EMAIL_BACKEND` | Django email backend path (ADR 0016). Empty with DEBUG on prints to the console. | DEBUG off | no | `anymail.backends.resend.EmailBackend` | spec |
| `RESEND_API_KEY` | Resend's API key, read when `EMAIL_BACKEND` is Resend's. | Resend, DEBUG off | yes | `<Resend API key>` | console |
| `DEFAULT_FROM_EMAIL` | The sender of every message. Empty with DEBUG on means `OPEN <noreply@localhost>`. | DEBUG off | no | `OPEN <noreply@mail.example.org>` | spec |
| `CLIENT_IP_HEADER` | The header the proxy sets to the client IP, for throttling. Default `do-connecting-ip`. | no | no | `do-connecting-ip` | default |
| `API_ACCOUNT_THROTTLE_PER_MINUTE` | API requests per Account per minute (ADR 0030). Default 120. | no | no | `120` | default |
| `API_ACCOUNT_THROTTLE_PER_DAY` | API requests per Account per day. Default 10000. | no | no | `10000` | default |
| `API_CLIENT_IP_THROTTLE_PER_MINUTE` | API requests per client IP per minute. Default 60. | no | no | `60` | default |
| `API_CLIENT_IP_THROTTLE_PER_DAY` | API requests per client IP per day. Default 3000. | no | no | `3000` | default |
| `API_ORGANIZER_REPORT_THROTTLE_PER_DAY` | Organizer Reports per Account per day (ADR 0037). Default 5. | no | no | `5` | default |
| `API_DATA_EXPORT_THROTTLE_PER_DAY` | Data export requests per Account per day (ADR 0051). Default 3. | no | no | `3` | default |
| `API_ORGANIZER_INVITE_THROTTLE_PER_DAY` | Organizer invites per Account per day (ADR 0054). Default 20. | no | no | `20` | default |
| `API_CLUB_INVITE_THROTTLE_PER_DAY` | Club admin invites per Account per day (ADR 0055). Default 20. | no | no | `20` | default |
| `PASSWORD_MIN_LENGTH` | The shortest password allowed, 8 to 128 (ADR 0047). Default 8. | no | no | `8` | default |
| `PASSWORD_BREACH_TIMEOUT_SECONDS` | The longest wait on the breached password check, 1 to 10. Default 3. | no | no | `3` | default |
| `SESSION_MAX_AGE_DAYS` | A signed-in session's lifetime. Default 30. | no | no | `30` | default |
| `SESSION_IDLE_DAYS` | Days unused before a signed-in session ends. Default 7. | no | no | `7` | default |
| `ORGANIZER_SESSION_MAX_AGE_DAYS` | Session lifetime for Organizer members and Club Roles (ADR 0057). Default 7, or `SESSION_MAX_AGE_DAYS` when lower. | no | no | `7` | default |
| `ORGANIZER_SESSION_IDLE_DAYS` | Idle limit for Organizer members and Club Roles. Default 1. | no | no | `1` | default |
| `SIGN_IN_DEVICE_COOKIE_DAYS` | How long a browser or app is remembered for sign-in emails. Default 365. | no | no | `365` | default |
| `TERMS_OF_SERVICE_VERSION` | The Terms of Service version sign-up requires (ADR 0026). Default in settings. | no | no | `2` | default |
| `PRIVACY_POLICY_VERSION` | The Privacy Policy version sign-up requires (ADR 0026). Default in settings. | no | no | `3` | default |
| `MATCH_CONFIRMATION_EXPIRY_DAYS` | Days before a pending match is voided (ADR 0013). Empty means never. | no | no | `30` | default |
| `MATCH_DISPUTE_EXPIRY_DAYS` | Days a disputed match waits for a resolution (ADR 0034). Default 30. | no | no | `30` | default |
| `MATCH_DUPLICATE_HOLD_DAYS` | Days a possible duplicate flag stays open (ADR 0034). Default 14. | no | no | `14` | default |
| `UNUSUAL_SUBMISSION_FIRST_MATCHES` | Unusual submission flag: an Organizer's first matches (ADR 0049). Default 25. | no | no | `25` | default |
| `UNUSUAL_SUBMISSION_BATCH_SIZE` | Unusual submission flag: matches in one upload. Default 100. | no | no | `100` | default |
| `UNUSUAL_SUBMISSION_DAILY_MATCHES` | Unusual submission flag: a submitter's matches in one UTC day. Default 200. | no | no | `200` | default |
| `UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES` | Unusual submission flag: a player's matches on one date (ADR 0052). Default 30. | no | no | `30` | default |
| `ORGANIZER_REVIEW_WINDOW_DAYS` | Days back the moderator review counts disputes and flags. Default 90. | no | no | `90` | default |
| `ORGANIZER_CREATE_LIMIT_PER_ACCOUNT` | Organizers an Account may be an admin of. Default 5. | no | no | `5` | default |
| `ORGANIZER_OPEN_INVITES_LIMIT` | Unexpired invites an Organizer may have. Default 50. | no | no | `50` | default |
| `ORGANIZER_INVITE_DAYS` | Days an Organizer invite link works. Default 7. | no | no | `7` | default |
| `ORGANIZER_STEP_UP_SECONDS` | How recent a second factor step must be for an authority change or upload (ADR 0057). Default 300. | no | no | `300` | default |
| `ORGANIZER_NEW_MEMBER_HOLD_HOURS` | Hours a new Organizer member waits for authority. Default 24; 0 turns it off. | no | no | `24` | default |
| `CLUB_CREATE_LIMIT_PER_ACCOUNT` | Clubs an Account may be an admin of (ADR 0055). Default 5. | no | no | `5` | default |
| `CLUB_OPEN_INVITES_LIMIT` | Unexpired invites a Club may have. Default 50. | no | no | `50` | default |
| `CLUB_INVITE_DAYS` | Days a Club invite link works. Default 7. | no | no | `7` | default |
| `PORT` | gunicorn's port. Default 8000. Process environment only. | no | no | `8000` | App Platform |
| `WEB_CONCURRENCY` | gunicorn worker processes. Default 2. Process environment only. | no | no | `2` | default |

`.env.example` explains each value's format and range, and `.claude/rules/settings.md` gives the details. These are not read from the environment:

- OAuth token lifetimes and scopes are fixed in `OAUTH2_PROVIDER` in `config/settings.py` (#28), not read from the environment.
- Sentry has no variables yet. #65 adds its rows here.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` are read by boto3, not settings, and only if email moves to Amazon SES (see [Email](#email)).

The PWA's static site (`app` in `.do/production.yaml`) gets two variables at build time only. The frontend reads them, not Django. `.do/sites.yaml` sets no variables.

| Build variable | Purpose | Value in the spec |
| --- | --- | --- |
| `PUBLIC_API_MOCK` | Turns the frontend's mock API off in the production build. | `off` |
| `APP_COMMIT` | The deployed commit the Profile page shows (#349). | `${_self.COMMIT_HASH}` |

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
- `app`: the PWA's static site (#304), built from the public repository `openpickleball/frontend` (`app/`, branch `main`) with `pnpm build && pnpm check:build`, serving `build/` with `index.html` for every path that has no file. Its build gets `PUBLIC_API_MOCK=off` and `APP_COMMIT` (the deployed commit, `${_self.COMMIT_HASH}`, #349), both at build time only. Its source is `git`, not `github`, so App Platform needs no access to the organization and a frontend merge never deploys anything: a frontend change goes live with a deploy of the production app, which also redeploys `web`, `worker`, and `beat` and runs `migrate`. The other way round, every deploy of the production app builds frontend `main` at its head. It ships whatever `main` holds, and if `main` fails its build (or the buildpack's Node version does not fit), the whole deploy fails, a backend-only change, a hotfix, or a secret change in the console included.
- `pg`: a managed Postgres cluster (`open-production-pg`), database `open`, user `open`.
- `valkey`: a managed Valkey cluster (`open-production-valkey`), the cache and Celery broker (ADR 0006). DigitalOcean's Valkey takes TLS only (`rediss://`).

Every service, worker, and job runs the same image on the smallest instance size, `apps-s-1vcpu-0.5gb`. The variables are set once, at app level, so every component gets them, and each is `RUN_TIME`, so none reaches the static build of `app`. `DATABASE_URL` is the pool `open-pool` (`${pg.open-pool.DATABASE_URL}`), `DIRECT_DATABASE_URL` the database itself (`${pg.DATABASE_URL}`), `REDIS_URL` the Valkey cluster (`${valkey.DATABASE_URL}`), and `DJANGO_ALLOWED_HOSTS` the two hosts, `api.openpb.org,app.openpb.org`. `CSRF_TRUSTED_ORIGINS` stays unset (see [Domains](#domains)). `FRONTEND_URL` is `https://app.openpb.org` (#182), where the links in verification and reset emails open the PWA. `CLIENT_IP_HEADER` stays at its default, `do-connecting-ip`.

The spec holds no secret values. `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` (see [Settings](#settings)), `RESEND_API_KEY`, and `ADMIN_URL` (the admin's private path, such as `staff-x7k2/`, never `admin/`) are declared as secrets with no value. Set them in the App Platform console, or in a spec exported from the running app, where App Platform shows them encrypted (`EV[...]`). Never commit a value.

App Platform's HTTP health probes reach the container directly, over plain http, with a Host header that `DJANGO_ALLOWED_HOSTS` refuses. `HealthProbeMiddleware` (`config/health_probe.py`, ADR 0042) answers a GET of exactly the two health paths in that case. Every other request keeps the host check and the https redirect.

#### Domains

The app answers on two hosts (ADR 0061, ADR 0063):

| Host | Paths that reach Django | Who uses it |
| --- | --- | --- |
| `api.openpb.org` (primary) | every path, the admin at `ADMIN_URL` included | partners, staff |
| `app.openpb.org` (alias) | `/api/`, `/_allauth/`, and `/oauth/` only | the PWA, same-origin |

Every other path on `app.openpb.org` goes to the PWA's static site, `app`, through its catch-all rule `/`, listed after the three Django rules. Each ingress rule names its host and keeps its path prefix. App Platform does not document the order it tries rules in, so on each host no rule comes after a shorter prefix of it, and first match and longest prefix give the same result. The admin is on `api.openpb.org` only. No rule routes the app's `ondigitalocean.app` host, and `DJANGO_ALLOWED_HOSTS` leaves it out, so Django refuses it (400) if a request there ever reaches it; App Platform's health probes go straight to the container and still pass. `CSRF_TRUSTED_ORIGINS` stays empty: listing either host would let its pages pass the origin check on the other. Adding a host means changing `domains`, `ingress`, and `DJANGO_ALLOWED_HOSTS` together; `config/tests/test_app_spec.py` checks that they agree.

DNS for `openpb.org` is not on DigitalOcean, so the spec sets no `zone`. At the DNS provider, each host is a CNAME to the app's `ondigitalocean.app` host, with the provider's proxy off (DNS only), so App Platform can issue the certificate and `do-connecting-ip` carries the player's IP. App Platform shows the host to point at under the app's Settings, Domains, once the app exists (#62).

#### Before the first deploy

The spec does not set these. #62 and #63 do:

- Create the Postgres and Valkey clusters with the cluster names above, in one region, and set the app's `region` to match. The spec leaves `region` unset.
- Create the pool `open-pool` in transaction mode on database `open` for user `open`: `doctl databases pool create <cluster-id> open-pool --size 10 --mode transaction --db open --user open`.
- Set Valkey's eviction policy to `volatile-lru`: `doctl databases configuration update <cluster-id> --engine valkey --config-json '{"valkey_maxmemory_policy":"volatile-lru"}'`. Valkey holds the cache and the Celery queues. Cache and throttle keys all expire, and queued tasks never do, so `volatile-lru` evicts only cache keys. An `allkeys-*` policy could drop queued tasks, and `noeviction` would make every cache write fail once Valkey is full.
- Make user `open` the owner of database `open`: as `doadmin`, run `ALTER DATABASE open OWNER TO open;`. On Postgres 15 and later the `public` schema belongs to the database owner, so a user that does not own the database cannot create tables, and the first migrate fails. Today both URLs use `open`, so the runtime user owns every table, the audit table included. #103 decides separate migration and runtime users, as the Audit log rules require.
- Limit each cluster's trusted sources to the app (#62, #103).
- Set the four secret values: `DJANGO_SECRET_KEY`, `FIELD_ENCRYPTION_KEYS` (a newly generated Fernet key, backed up apart from the database), `RESEND_API_KEY`, and `ADMIN_URL` (a path only staff know, letters, digits, hyphens, and underscores ending in `/`).
- Give App Platform access to the image on GHCR, and deploy by digest instead of the `main` tag (#63).
- On the first deploy, check the PWA's build. Check which Node version it uses: without `engines.node` in `app/package.json`, the buildpack uses its default (22.x at the time of writing), not the repository's `.nvmrc` (26), which sits outside `source_dir: app`. The fix for that belongs in the frontend, whose README lists its own checks. Check also that Profile, Help and about, App version on `app.openpb.org` shows the deployed commit. DigitalOcean documents bindable variables at build and run time in general (Dockerfile builds aside) and lists `${_self.COMMIT_HASH}` for static sites, but that this one is filled at build time is unchecked. If it is not, the frontend ignores the value and shows the version alone.
- On the first deploy, confirm that `${valkey.DATABASE_URL}` is Valkey's `rediss://` connection string. DigitalOcean documents `DATABASE_URL` for every database and `REDIS_URL` only for its older Redis engine.

#### Pool sizing

One instance each of web, worker, and beat opens at most 5 client connections: web 2 (`WEB_CONCURRENCY`, default 2), worker 2 (`--concurrency 2`), and beat 1. The smallest cluster (1 GiB) allows 25 connections, 3 of them reserved, so 22 are usable. DigitalOcean suggests a pool of about half the usable connections, so `open-pool` has size 10. That leaves room for a second web instance and for the old and new instances running side by side during a deploy. Migrations use one direct connection, outside the pool.

#### Validate the spec

```bash
doctl apps spec validate --schema-only .do/production.yaml
doctl apps spec validate --schema-only .do/sites.yaml
```

`--schema-only` checks the fields locally. A full `doctl apps spec validate`, which also checks the bindings and the instance size, needs an authenticated doctl. Run it before creating each app (#62).

#### Check the client IP header

The per-IP throttle reads the client IP from `do-connecting-ip` (ADR 0030). Until this check passes on production, no public operation goes live: a client that can set that header itself could spread its requests across any number of throttle buckets.

1. On the production `web` component, before it opens to players (#68), set `GUNICORN_CMD_ARGS` to `--access-logfile - --access-logformat "%({http_do_connecting_ip}e)s %(s)s"` and redeploy. gunicorn applies `GUNICORN_CMD_ARGS` after `gunicorn.conf.py`. The format logs the header's value as Django reads it (gunicorn joins repeated headers with a comma) and the status, never the request line, which can hold an email verification or password reset key.
2. From a machine whose public IP you know, run `curl -sS -o /dev/null -H 'do-connecting-ip: 203.0.113.7' https://<app domain>/api/v1/health/live`.
3. Read the web runtime logs with `doctl apps logs <app-id> web --type run`. The check passes only if the logged value is exactly your real IP. A comma-separated list, or `203.0.113.7`, fails it.
4. Remove `GUNICORN_CMD_ARGS` and redeploy, since the line logs client IPs.
5. Record the result on #62. If the check fails, no public operation goes live until the setting or the throttle changes.

#### Monthly cost

Prices from DigitalOcean's pricing pages, checked 2026-11-27:

| Component | Size | Per month |
| --- | --- | --- |
| web | `apps-s-1vcpu-0.5gb` (512 MiB, shared CPU) | $5.00 |
| worker | `apps-s-1vcpu-0.5gb` | $5.00 |
| beat | `apps-s-1vcpu-0.5gb` | $5.00 |
| migrate job | `apps-s-1vcpu-0.5gb`, billed per second while it runs | under $0.01 per deploy |
| app (static site) | free in a paid app (DigitalOcean: "Static sites in a paid app are $0"); its outbound transfer counts against the pooled allowance of the containers (50 GiB per $5 container, $0.02 per GiB over) | $0.00 |
| Postgres | 1 GiB RAM, 1 vCPU, single node, from 10 GiB storage | $15.15 |
| Valkey | 1 GiB | $15.00 |
| Total (production app) | | $45.15 |
| sites app (`open-sites`, billed apart) | static sites only, on the free tier | $0.00 |

The sites app is billed apart from the production app. It costs $0.00 while it stays within the free tier: up to 3 apps with only static sites per team, each with 1 GiB of outbound transfer a month, and $0.02 per GiB past it.

There is no staging environment, so the two apps together are the whole hosting cost: $45.15 a month while the sites app stays free. Each $5 container includes 50 GiB of outbound transfer, pooled across the app, and more costs $0.02 per GiB. Running beat inside the worker (`celery worker -B`) would save $5 a month, but decision Q16 in #1 keeps beat a separate process.

#### Sites app

`.do/sites.yaml` (app `open-sites`) serves OPEN's static sites that need no API, in a second app apart from production (#304):

| Host | Component | Source |
| --- | --- | --- |
| `openpb.org` (primary) | `site` | `openpickleball/frontend` `site/`, built with `pnpm build`, serving `public/` |
| `www.openpb.org` (alias) | none | a 301 redirect to `https://openpb.org` |
| `dev.openpb.org` (alias) | `developers` | `openpickleball/developers`, built with `zensical build --strict`, serving `site/` |

It holds only static sites, so it stays on the free tier (see [Monthly cost](#monthly-cost)). It must never hold a service, worker, job, or database. The partner docs (`openpickleball/partner-docs`, `partner.openpb.org`) join as a component named `partner` once #184 creates the repository. The status page stays on GitHub Pages, outside DigitalOcean (#139).

Each source is `git`, not `github`: the repositories are public, so App Platform needs no access to the organization, and a merge in either repository never deploys the sites app. A site change goes live with a deploy of the sites app. Each deploy builds both repositories at the head of `main`, so a `main` that fails its build fails the whole deploy. The sites keep loading while the production app is stopped.

The public site's build copies `/.well-known/funding-manifest-urls` from the frontend repository's root into its output. Files outside `source_dir` are there during the build, not at run time. Each site answers a missing path with its own `404.html`.

DNS stays at the openpb.org DNS provider. The apex needs CNAME flattening there, or App Platform's A records for the app; it does not need DigitalOcean nameservers. Lower the TTLs before the move, so pointing DNS back at GitHub Pages is quick.

Static sites cannot set response headers, in this app or in the production app's `app`. So the spec cannot set Cache-Control (for `index.html`, the PWA's `service-worker.js`, and its manifest), the service worker's own policy, or framing protection (`frame-ancestors`, `X-Frame-Options`). Whatever App Platform sends by default applies, and its CDN cache cannot be turned off.

Check on the first deploy:

- No rule routes the app's `ondigitalocean.app` host, so the redirect, well-known file, and 404 checks below can run only once DNS points at the app. Lower the TTLs first, as above.
- Which Node version the public site's build uses. Without `engines.node` in `site/package.json`, the buildpack uses its default (22.x at the time of writing), not the repository's `.nvmrc`. The fix belongs in the frontend.
- Which pnpm version it uses: the one in `packageManager`, through Corepack.
- That the devDependencies (Tailwind among them) are still installed when `build_command` runs.
- That the Python buildpack builds the developer site with uv (it needs `pyproject.toml`, `uv.lock`, and `.python-version`, and no other package manager file). DigitalOcean does not document this for a static site. `zensical` must also be on `PATH` when `build_command` runs; the developers repository's CI runs `uv run zensical build --strict`. If either fails, build the site in GitHub Actions and deploy the output instead, and record which on #304.
- That `www.openpb.org/some/path` redirects to `openpb.org/some/path`, keeping the path.
- That `openpb.org/.well-known/funding-manifest-urls` serves the copied file.
- That a missing path on each host gets that site's 404 page.

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
