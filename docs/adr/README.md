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

Many decisions are already made in GitHub issue #1 (the backend plan). Records turn them into permanent documents. Choices from issue #1 that may deserve a record later: how the append-only Ledger is enforced, incremental updates versus replay (Q6), and engine pinning (Q4). The partner Application model (Q3) is recorded in ADR 0010.

## Index

Add each record here when it merges.

- [0001. Why Django](0001-django.md) (issue #16)
- [0002. Player identity: no unclaimed players, and no opt-out](0002-player-identity.md) (issue #18)
- [0003. Email identity for Accounts](0003-email-identity-for-accounts.md) (issue #3)
- [0004. Settings from the environment, secure by default](0004-settings-from-the-environment.md) (issue #4)
- [0005. API versioning and error format](0005-api-versioning-and-error-format.md) (issue #5)
- [0006. Celery queues and broker](0006-celery-queues-and-broker.md) (issue #9)
- [0007. An append-only audit log enforced in the database](0007-append-only-audit-log.md) (issue #17)
- [0008. Public identifiers: OPEN Codes](0008-public-identifiers.md) (issue #92)
- [0009. Personal data: what OPEN collects and how it is protected](0009-personal-data.md) (issue #93)
- [0010. Partner applications: a swapped OAuth Application model](0010-partner-application-model.md) (issue #22)
- [0011. Duplicate match detection](0011-duplicate-match-detection.md) (issue #25)
- [0012. Match submission permissions](0012-match-submission-permissions.md) (issue #79)
- [0013. Match confirmation and changes](0013-match-confirmation-and-changes.md) (issue #26)
- [0014. Headless account authentication](0014-headless-account-authentication.md) (issue #30)
- [0015. First-party match endpoints](0015-first-party-match-endpoints.md) (issue #31)
- [0016. Email through an outbox and the email queue](0016-email-outbox.md) (issue #35)
- [0017. The signup age screen and the 13-and-older rule](0017-signup-age-screen.md) (issue #114)
- [0018. Player search by display name](0018-player-search-by-display-name.md) (issue #191)
- [0019. Gender division required at signup](0019-gender-division-required-at-signup.md) (issue #215)
- [0020. An age check before the rest of signup](0020-signup-age-check.md) (issue #223)
- [0022. First and last name and a place at signup](0022-name-and-place-at-signup.md) (issue #221)
- [0023. Birth year and month and gender division are set once](0023-birth-and-division-set-once.md) (issue #225)
- [0024. Engine match type, confirmers, and a stored rating format](0024-engine-match-type-confirmers-and-rating-format.md) (issue #44)
- [0025. Rating state, rating history, and replay provenance](0025-rating-state-history-and-replay-provenance.md) (issue #45)
- [0026. Accepting the legal agreements at signup](0026-legal-agreements-at-signup.md) (issue #222)
- [0027. Live rating updates and the engine snapshot](0027-live-rating-updates-and-engine-snapshot.md) (issue #46)
- [0028. Rating replays after match changes](0028-rating-replays-after-match-changes.md) (issue #47)
- [0029. Moderator review in the admin](0029-moderator-review-in-the-admin.md) (issue #40)
- [0030. API throttling per Account and per client IP](0030-api-throttling.md) (issue #34)
