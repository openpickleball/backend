# Architecture decision records

Each file in this directory records one significant decision about how the backend is built, and why. Records explain the reasoning to future contributors, so that a decision can be questioned on its merits instead of guessed at.

## When to write one

Write a record when a choice is hard to reverse or affects more than one part of the system. Examples: language and framework, database, how the backend calls the engine, authentication, API versioning, how the Ledger is stored, data retention and deletion.

Small, local choices belong in code comments or the pull request description.

## How to write one

1. Copy the template below into a new file named `NNNN-short-title.md`, using the next unused number.
2. Set the status to `Proposed` and open a pull request linked to an issue.
3. When the pull request merges, change the status to `Accepted`.
4. Never edit the decision of an accepted record. To change it, write a new record and set the old one's status to `Superseded by NNNN`.

## Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD

## Context

What problem or question needs a decision, and what constraints apply (charter, privacy, security, volunteer capacity).

## Decision

What we chose, stated plainly.

## Consequences

What becomes easier, what becomes harder, and what we will need to revisit.

## Alternatives considered

Each option we rejected, and why.
```

## Planned records

Many decisions are already made in GitHub issue #1 (the backend plan). Records turn them into permanent documents:

- 0002: unclaimed players, claiming, and opt-out (issue #18; decision Q9)

Other choices from issue #1 that may deserve a record later: how the append-only Ledger is enforced, incremental updates versus replay (Q6), engine pinning (Q4), and partner API authentication (Q2, Q3).

## Index

Add each record here when it merges.

- [0001. Why Django](0001-django.md) (issue #16)
- [0003. Email identity for Accounts](0003-email-identity-for-accounts.md) (issue #3)
- [0004. Settings from the environment, secure by default](0004-settings-from-the-environment.md) (issue #4)
- [0005. API versioning and error format](0005-api-versioning-and-error-format.md) (issue #5)
- [0006. Celery queues and broker](0006-celery-queues-and-broker.md) (issue #9)
- [0007. An append-only audit log enforced in the database](0007-append-only-audit-log.md) (issue #17)
