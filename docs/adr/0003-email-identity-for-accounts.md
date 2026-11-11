# 0003. Email identity for Accounts

- Status: Proposed
- Date: 2026-11-10

## Context

OPEN logs in by email. Django's custom user model must be chosen before the first migration (issue #3), because swapping it later means rewriting migrations. An Account (see `CONTEXT.md`) is a registered login. It is separate from a Player, so the login record holds no player profile data.

Email identity is a security-sensitive area: identity linking and organizer verification depend on one address meaning one Account. Review of issue #3 found that case and Unicode rules that disagree lead to real problems:

- Postgres `UPPER()` and `LOWER()` disagree for some letters, such as the dotless i. A lookup with `email__iexact` matched two Accounts that the unique constraint treated as distinct, and login crashed.
- Unicode lookalikes, such as `ınfo@example.org` next to `info@example.org`, look identical to an organizer or admin but are separate Accounts.
- Unicode normalization (NFKC) can change an address after validation, making it too long or malformed.

## Decision

- `accounts.User` extends `AbstractUser`, with `email` as `USERNAME_FIELD`. It has no `username`, `first_name`, or `last_name`.
- Forms (through the model's `clean()`), the manager, and lookups normalize every email: NFKC, strip surrounding whitespace, then lowercase one character at a time. Lowering one character at a time avoids Python's final-sigma rule, which Postgres `LOWER()` does not apply.
- The normalized address must be ASCII, at most 254 characters, and a valid email address. One field validator applies all three rules to the normalized value, and the manager applies the same rules.
- The database enforces the 254-character column length, uniqueness on `Lower("email")`, and an ASCII-only check constraint. Writes that skip the manager and the field validator (`save()` without `full_clean()`, `bulk_create`, `update`) cannot break those rules, but they do skip normalization, so Accounts are created through the manager or a form.
- Identity lookups (the manager's natural-key lookups, used by login) use the same `Lower("email")` expression as the unique constraint, so at most one Account can match.

## Consequences

- Through the manager and forms, one address means one Account, and lookalikes that use non-ASCII characters cannot be created. ASCII lookalikes, such as `rn` for `m`, are still possible.
- Internationalized email addresses are not supported. Allowing them later needs a lookalike strategy, a validator change, and a migration that relaxes the check constraint.
- The local part is lowercased even though mail standards allow it to be case-sensitive. We accept that trade-off so that case variants cannot create separate Accounts.
- Code that finds an Account by email must go through the manager's natural-key lookup, not `email__iexact`.

## Alternatives considered

- **Keep the default username field.** OPEN has no use for usernames, and two login identifiers would invite confusion.
- **A case-insensitive `citext` column.** It needs a Postgres extension migration, and it would not solve lookalikes or normalization.
- **Allow internationalized addresses and detect confusable characters.** This is harder to get right, and it is not needed before public signup exists.
