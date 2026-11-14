# 0007. An append-only audit log enforced in the database

- Status: Proposed
- Date: 2026-11-13

## Context

Every manual rating or match change and every moderation action must be traceable (issue #17). Ratings computed by a Replay are traced through replay provenance instead (hard rule 4). Later issues record admin actions, manual rating and match edits, dispute outcomes, MFA and password changes, ended sessions, and OAuth application requests and decisions. A record that the app, or a bug in it, can quietly change or delete is not evidence. Audit events also must never hold passwords, tokens, secrets, or recovery codes.

## Decision

- One `AuditEvent` model in `apps/audit`: timestamp, actor Account (nullable), action, target (content type and id), before and after JSON, and reason.
- Code writes events only through `record_event()`, inside the same transaction as the change. It redacts values under sensitive key names. `snapshot()` builds `before` and `after` from an explicit field list, in the form they will be stored.
- In app code, the ORM refuses every change and delete of a row, before any SQL runs.
- A Postgres trigger refuses `UPDATE` and `DELETE` of any row, and `TRUNCATE` of a non-empty table. The trigger checks the table that fired it, pins its own `search_path` and turns row-level security off, so a temporary table or a policy cannot fool its emptiness check. It allows `TRUNCATE` of an empty table only under READ COMMITTED, so Django's `flush` still works while no audit row exists.
- The admin shows events read-only. Nobody, superusers included, can add, change, or delete one.
- `actor_user` and `target_content_type` use `PROTECT`. `SET_NULL` would need an `UPDATE`, which the trigger refuses.

## Consequences

- Rows survive app bugs and admin mistakes. The database refuses raw `UPDATE`, `DELETE`, and `TRUNCATE` from the app's role.
- The trigger does not stop the table's owner or a database superuser: either can disable it. The runtime role must therefore not own the table. This is a requirement on the database role work in #103 (a comment there asks for it).
- An Account that has acted cannot be hard-deleted, and a content type an event points at cannot be removed. Account deletion work must plan around this.
- `flush`, and the teardown of a transactional test, fails once the table has a row.
- A migration that issues `UPDATE` or `DELETE` on existing rows (a data migration, or an `AlterField` that backfills a default) fails unless it handles the trigger explicitly. Inside a migration only the trigger protects the rows: historical models have neither the ORM guards nor the custom manager.

## Alternatives considered

- **Model and admin checks only.** Any raw SQL, bulk operation, or bug would get around them.
- **Revoking UPDATE and DELETE from the app role only.** Locally and in CI the app role is a database superuser (and owns the table), so a REVOKE does nothing there. It is still wanted as a second layer, and the comment on #103 asks for it.
- **A trigger that always refuses TRUNCATE.** It would break `flush` for every transactional test, including ones that never touch the audit log.
- **Storing only changed fields.** Full before and after values are simpler to read, and callers choose the fields with `snapshot()`.
