# 0008. Public identifiers: OPEN Codes

- Status: Accepted
- Amended by: [ADR 0036](0036-csv-results-files.md) (prefix `F`, length 8, for a Results File)
- Date: 2026-11-14

## Context

Players, clubs, events, organizers, and matches need public identifiers that partners, exports, webhooks, and URLs can use (issue #92). Partner mappings, player data exports, and partner databases will store them, so an identifier must never change once issued. The scheme must be fixed before the first Player migration (#19).

The default primary key is a `BigAutoField`. If the row number were the public identifier, anyone could list every player by counting, which works against profile visibility and opt-out. It would also reveal how many players and matches exist.

Players and organizers will read codes aloud and type them. A typo must be rejected, not resolved to another player.

## Decision

- `BigAutoField` stays the internal primary key and the foreign key target. Row ids never appear in the API: no path, query, body, or response carries one. A test walks the OpenAPI document, hidden operations included, and fails on any parameter or property that is a number, or an array of numbers, and whose name marks it as a row id. String-typed ids are out of its scope, since a string cannot be told apart from a code.
- Each public record gets an OPEN Code in a `public_code` column, from the abstract model `apps.public_ids.models.PublicCodeModel`. The column is unique (a database constraint and index) and not editable.
- Format: a type prefix, a hyphen, then the body in groups of 4 characters joined by hyphens. The canonical form is uppercase. Examples (made up): player `P-7K3M-QX9H`, club `C-4HWT-2N8Z`, match `M-9QF6-RD2K-X3BP`.
- Alphabet: Crockford base32, `0123456789ABCDEFGHJKMNPQRSTVWXYZ` (no I, L, O, or U).
- Prefixes: `P` player, `C` club, `E` event, `G` organizer, `M` match.
- Lengths, not counting the prefix: 8 characters (7 random plus 1 check) for players, clubs, events, and organizers; 12 characters (11 random plus 1 check) for matches.
- Check character: Luhn mod 32 over the prefix and the random characters. It catches every single-character substitution, and so a code moved to another type. It catches most swaps of two neighboring characters.
- Generation: the random characters come from `secrets`, never from the row id or the time.
- Registry: every issued code is stored in `IssuedCode`, with a unique constraint. A code is never issued again, even after its record is deleted or opted out. The ORM refuses to change or delete a registry row.
- Collisions: on create, a new code is inserted into the registry inside a savepoint. A code that is already in the registry, or that a record of the same model already holds, is a collision. A collision is retried with a fresh code, up to 5 attempts, then `PublicCodeExhausted` is raised. The record insert runs in the same transaction, so a failed insert also rolls back its registry row.
- A code never changes. `save()` refuses a changed code on an existing record, and refuses a code set by hand on a new one. Saving an existing record only ever updates its row: it raises if the record was deleted or its row is gone, and it compares the code with the one stored for the pk being written. If a create fails, even at commit, the instance is left new, so a retry issues a fresh code.
- Input normalization, in order: refuse anything that is not ASCII or is longer than 64 characters; uppercase; drop spaces and hyphens; check the prefix (the endpoint's prefix may be omitted, any other prefix is refused); map I and L to 1 and O to 0; refuse characters outside the alphabet; verify the length and the check character. The result is the canonical form. A code typed without its prefix is checked as the endpoint's type. A code of another type typed that way fails the length check or the check character.
- API: a path parameter is `Annotated[str, PublicCodePath(CodeType.X)]`. `PublicCodePath` is for path parameters only, not body or response fields. A code path parameter that fails returns 404, not 422. This narrows ADR 0005's rule that validation errors return 422, for this parameter only. When the operation's other inputs are valid, a malformed code and a well-formed code that matches no record get the same 404 `ErrorResponse`. When another input is invalid, a malformed code still gets 404, but a well-formed code gets the 422 for that input, whether or not a record has it. So no response reveals whether a record exists. A response can reveal only whether a code is well formed, which anyone can compute from the published check algorithm.

### Collision math

A code with 7 random characters carries 35 bits: about 34 billion codes. With 1 million players issued, a new code hits an existing one about 1 time in 34,000. The retry absorbs that. Five collisions in a row would mean something is wrong. Match codes carry 55 random bits (about 3.6 times 10^16 codes).

### Longer codes later

If a type ever needs more codes, it gets a new, longer length. Existing codes stay valid and are never rewritten. Normalization then accepts every length that type has ever issued. The column's `max_length` is 32 to leave room.

### External IDs

A standard external rating system uses 6-character player IDs. An OPEN Code has a prefix and 8 or 12 characters, so an external ID never has its shape and is refused as malformed, never looked up.

## Consequences

- Partners can store codes forever, and a code cannot be guessed by counting.
- Fixtures (`loaddata`), `bulk_create`, `QuerySet.update`, and raw SQL skip issuance and the no-change rule, and must not set codes. Create records through `save()` or `objects.create()`. The field's default is None, so a write that skips `save()` and sets no code fails on NOT NULL. A code written another way has no registry row; issuance still treats it as a collision while a record holds it.
- To copy a record, set `public_code = None`, `pk = None`, and `_state.adding = True` before saving. The copy gets a fresh code.
- The registry grows forever. Its rows are small.
- There is no database trigger on the registry. Raw SQL can delete its rows, so the ORM guard protects only app code.
- Lookups compare the canonical form exactly, so normalization must run first. The path parameter type does that.

## Alternatives considered

- **UUIDs.** Long, hard to read aloud or type, with no check character.
- **Hashids or Sqids of the row id.** Reversible with the salt, and they still leak counts and order.
- **No check character.** A typo could resolve to another player.
- **No registry.** A deleted record's code could be issued again and point a partner's stored mapping at someone else.
- **A pattern in the OpenAPI schema.** Ninja would enforce it with a 422, so a malformed code would not get the same 404 as an unknown one.
