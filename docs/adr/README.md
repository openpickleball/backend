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

## Open decisions

These need records before or alongside the first code:

- Language and web framework (Python is likely, not final)
- Database and how the append-only Ledger is enforced
- How the backend depends on the engine (package version pinning, replay scheduling)
- Authentication for Accounts, Organizers, and Partners
- API versioning, and how this repository relates to the separately published Apache-2.0 API specifications

## Index

No records yet.
