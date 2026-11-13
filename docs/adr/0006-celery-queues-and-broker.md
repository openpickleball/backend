# 0006. Celery queues and broker

- Status: Proposed
- Date: 2026-11-12

## Context

Rating updates, email, and webhook delivery must run outside requests (issue #9). Each has a different failure mode. A large results import must not delay rating updates. A slow email provider must not hold up anything else. A slow partner endpoint must not delay ratings or email (decision F10 in issue #1). Queued messages carry the task name and the queue, so the queue layout is hard to change once tasks exist and messages sit in the broker.

The broker is Valkey (decision Q11), which DigitalOcean provides as one managed instance. The backend already reads one `REDIS_URL` and checks it strictly at startup (ADR 0004).

## Decision

- Celery runs with four queues: `default`, `ratings`, `email`, and `webhooks`. A task is routed by the start of its name: `apps.ratings.tasks.*` to `ratings`, `apps.emails.tasks.*` to `email`, `apps.webhooks.tasks.*` to `webhooks`, and every other task to `default`. Each queue has its own routing key, and sending to a queue that is not declared is an error, not a new queue.
- The broker is `REDIS_URL`, the same Valkey database as the cache. Celery's own broker and result variables (`CELERY_BROKER_URL` and the others) are refused when they have a value in the environment, so the URL always passes the same checks. A `rediss://` URL verifies the server certificate. A raw `;` in the URL's password is encoded before Celery reads it, because Celery would split the URL there into failover hosts.
- Tasks are acknowledged late, each worker process prefetches one task at a time, and each task gets a time limit (300 seconds soft, 360 hard) below the broker's one-hour visibility timeout.
- There is no result backend. A task that must report an outcome writes it to the database.
- Periodic tasks use django-celery-beat's database scheduler.
- Tests run tasks eagerly with errors propagated, so the test run needs no broker.

## Consequences

- Every task must be safe to run twice: late acknowledgement redelivers a task after a worker crash, and the visibility timeout redelivers a task whose time limit, countdown, or ETA passes one hour.
- Clearing the cache (`RedisCache.clear()` runs FLUSHDB) also drops every queued task. Splitting the broker into its own database or instance means a new setting and a deploy change.
- A task named outside its app's pattern goes to `default` without an error. New apps that send email or webhooks put those tasks in `apps.emails.tasks` and `apps.webhooks.tasks`, or add a route.
- Eager tests skip routing, the broker, acknowledgement, and retry delays. Routing has its own tests; anything that depends on real delivery needs a live check.
- Only one beat process may run, or every periodic task is sent twice.
- Prefetch is set for the whole worker. Production can run separate workers per queue (issue #61) without changing these settings.

## Alternatives considered

- One queue for everything: simplest, but a large import or a slow provider would delay rating updates.
- A separate broker URL: more isolation, but a second managed instance or variable for little gain at this size. It stays possible later.
- A result backend: nothing reads task results, and it would add keys and expiry jobs to the broker.
