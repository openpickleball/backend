# 0038. Postgres connection pool

- Status: Proposed
- Date: 2026-11-25

## Context

DigitalOcean managed Postgres limits how many connections a cluster accepts. Each gunicorn worker, each Celery worker process, and beat holds its own connection, so instances added under load can reach that limit (issue #60). DigitalOcean offers PgBouncer connection pools in front of the cluster. A pool in transaction mode lets many clients share a few server connections, but it gives each transaction a server connection that may differ from the last one. Anything kept on a connection between transactions is then lost or seen by another client: prepared statements, session settings (`SET`), `LISTEN`, temporary tables, `WITH HOLD` and other named cursors, and session advisory locks.

A pool's connection string names the pool as its database, not the database itself. Schema changes in `migrate` are long and take locks, and are better run on a direct connection than through a pool shared with live traffic.

The backend already fits transaction mode in most ways. Its advisory locks are transaction level (`pg_advisory_xact_lock`), its row locks end with their transaction, and no app code uses `SET`, `LISTEN`, temporary tables, or `.iterator()`. Django's Postgres backend prepares no statements unless `OPTIONS` asks for it. But django-environ copies a URL's query into `OPTIONS` and `CONN_MAX_AGE`, so a URL could turn prepared statements or persistent connections on.

## Decision

- `DATABASE_URL` may point at a pool in transaction mode. When it does, `DIRECT_DATABASE_URL` names the database itself. Setting `DIRECT_DATABASE_URL` (non-blank) turns on pooled mode. Empty, settings are as before, with one `default` database.
- In pooled mode, `default` (the pool) has `CONN_MAX_AGE` 0 and `DISABLE_SERVER_SIDE_CURSORS`, whatever the URL's query says, and settings refuse a `DATABASE_URL` whose query sets `prepare_threshold`, `assume_role`, or `pool`. The error names the variable and the option, never the value.
- Pooled mode adds a `direct` database alias from `DIRECT_DATABASE_URL`, checked like `DATABASE_URL`, with `CONN_MAX_AGE` 0. Tests mirror it to `default`, so the test run has one test database.
- OPEN's `migrate` command (`apps/database`) defaults `--database` to `direct` when that alias exists. An explicit `--database` wins. Every data migration queries through `schema_editor.connection.alias`, so it runs on migrate's connection and in the migration's transaction.
- Requests, tasks, and beat use `default`, the pool. Nothing in the app keeps session state on a connection.
- `compose.yaml` has an optional `pgbouncer` profile: PgBouncer in transaction mode, prepared statements off, a small pool, on 127.0.0.1:6432, so session state bugs show locally.

## Consequences

- The pooled-mode protections (`CONN_MAX_AGE` 0, no server-side cursors, refused URL options) apply only when `DIRECT_DATABASE_URL` is set. A pool URL in `DATABASE_URL` without it runs unprotected.
- New code must never rely on session state: only transaction level advisory locks, and no `SET` or `SET SESSION`, `LISTEN`, temporary tables, `WITH HOLD` cursors, or prepared statements. `SET LOCAL` inside a transaction is fine.
- A data migration that queries through a plain manager, or calls Django's `create_permissions` without `using`, goes through the pool instead of migrate's connection. A test calls each first-party data migration with a missing alias to catch this.
- Each request and task opens a new connection to the pool. That is cheap with PgBouncer, which keeps its server connections open.
- A lock wait holds a pool server connection for the whole wait. The pool must be sized for the busiest moment, waits included, or clients queue in PgBouncer.
- Direct connections for migrations count against the cluster's own limit, outside the pool.
- The deploy (#61) sets both variables. Leaving `DIRECT_DATABASE_URL` empty with a pool in `DATABASE_URL` runs migrations through the pool.

## Alternatives considered

- Passing `--database direct` by hand: easy to forget in a deploy step, and a forgotten flag silently runs migrations through the pool.
- A database router that sends migrations to `direct`: routers decide where a model migrates, not which connection runs `migrate`, so `migrate` would still record its history on `default`, and every app would need routing rules.
- Setting `DATABASE_URL` to the direct URL only in the migrate job: works, but needs a second environment for one job, and any other command run in the app's environment would still use the pool.
- Session mode pooling: keeps session state, but gives one server connection per client, so it does not raise the connection limit.
- psycopg's own pool in each process: it pools per process only, so the cluster still sees one connection per process.
