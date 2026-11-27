# 0051. Player data export

- Status: Proposed
- Date: 2026-11-26

## Context

The charter says players can always export their data, and ADR 0009 says the export holds every field in the inventory that belongs to the player, and that the file is deleted when its download link expires or at Account deletion, whichever comes first. Issue #39 asks for `POST /me/export`, a task that builds a JSON file, an email with a short-lived, single-use download link that works only for the owner, no private data of other players, a documented format, and an export that contains every model that references the player.

The export holds the most sensitive data OPEN keeps in one place: the decrypted legal name, birth year and month, and gender division (ADR 0046), sessions with IP addresses (ADR 0047), and the Account's Audit Events. The maintainer decided five questions on the issue.

## Decision

This adds to [ADR 0009](0009-personal-data.md) (how the export is stored and deleted) and [ADR 0048](0048-account-deletion.md) (Account deletion deletes the export).

### The maintainer's decisions

1. **Storage.** A `DataExport` row (`apps/exports`) holds the JSON, encrypted with the field encryption keys (`EncryptedTextField`, ADR 0046). No object storage.
2. **Lifetime and limits.** The link lasts 24 hours. The first successful download deletes the row. A new request replaces an earlier export. A 15-minute beat entry (`sweep-data-exports`) deletes expired rows. The request has its own day-window throttle, `API_DATA_EXPORT_THROTTLE_PER_DAY` (default 3), its only throttle, like the Organizer report throttle (ADR 0037).
3. **The link.** The email links to `{FRONTEND_URL}/account/export/{key}`. That page calls `POST /api/v1/me/export/download` with the key in the body, behind sign-in and the Account throttle. The key is stored hashed and must belong to the signed-in Account. The request needs a recent reauthentication (ADR 0048's 403 `reauthentication_required`); the download does not.
4. **Audit Events.** The export holds the events that target the Account, its Player, its privacy row, its rating states, or its agreement rows, plus the events the Account performed. Each shows its action, time, target type, and the target's OPEN Code (null when it has none). `before` and `after` appear only when the target is one of those own records. The actor appears only as `you`, `staff`, `other`, or `system`. The reason never appears. Match status events come through the matches section, so an event targeting a Match appears only when the Account performed it, without values.
5. **Scope.** The account (email, joined, last login), its email addresses with verified and primary status, its sessions (IP address, user agent, created, last seen; never the key or the row id), its MFA methods (type and dates; never a secret or the `data` field), Organizer memberships as ADR 0050 shows them (code, name, verified, role), Organizer Reports it filed (Organizer code, reason, created, closed; never the close reason or anything about the closer), vouches, Submission Batches and results files it submitted (codes and dates), Partner Applications it owns (client_id and name; never the secret), and its agreement rows. Plus the Player's profile with the decrypted legal name, birth year and month, and gender division (the player's own only), privacy settings, matches played, confirmations, ratings, and rating history. Other players appear only as match responses show them (`match_out` with the Account as viewer: OPEN Code and display name, or "Private player").

### Choices made within those decisions

- **Every model that references the player.** The export also holds the Account's Club Roles (Club code and name, role), the Matches it submitted (code and submitted time only), and the OAuth tokens and grants issued to it (kind, application `client_id`, scope, created, expires; never a token, code, or secret). A test walks every installed model with a foreign key or one-to-one link to the Account model or `players.Player` and requires each to be exported or listed in an exclusion map with a reason: the admin log (staff history, covered by the Account's own `admin.*` events), `Organizer.verified_by` and `PartnerApplication.approved_by` (covered by the events the Account performed), and `DataExport` itself.
- **No Player.** An Account with no Player still gets an export; the Player sections are null or empty.
- **The key.** `secrets.token_urlsafe(32)`, made by the build task and stored only as its SHA-256 hex digest (unique). It exists only in the queued email (deleted once sent) and the player's mailbox. It is never logged, put in a task argument, or recorded in an Audit Event. A key that is empty, longer than 128 characters, unknown, expired, used, not yet built, or another Account's gets one fixed 404. The length limit is the service's, not the schema's, so a long key gets the same 404, not a 422.
- **Format.** A set of ninja Schemas (`DataExportOut` and its sections, reusing `MyPlayerOut`, `MatchOut`, `RatingOut`, `RatingHistoryItemOut`, and `OrganizerMembershipOut`), so the OpenAPI document describes it, plus `docs/data-export.md`. It carries `format_version` `"1"` and `generated_at`. The download returns the stored bytes as `application/json` with `Content-Disposition: attachment; filename="open-data-export.json"` and `Cache-Control: no-store`.
- **No row ids.** `before` and `after` drop every key that holds a row id: keys named like one (`id`, `pk`, `*_id`), a rating change's `replay`, and an admin change's `groups` and `user_permissions`. The API never returns a row id (ADR 0008).
- **Actor kinds.** `you` is the Account itself, `staff` another Account that is staff or a superuser when the export is built, `other` any other Account or a Partner Application, and `system` no actor.
- **Answers.** The request answers 202 with `requested_at`. The throttle runs before the reauthentication check (ninja checks throttles before the view), so a request refused with `reauthentication_required` counts against the day limit, as a 422 does elsewhere (ADR 0030). The lifetime is a constant (`LINK_LIFETIME`, 24 hours), not a setting.
- **Lost tasks.** The build task gets only the row id. A pending row whose task was lost is queued again by the sweep once it was last queued more than an hour ago (`REQUEUE_AFTER`, the pattern of the results file sweep), and a pending row older than the link lifetime is deleted.
- **Audit.** One `data_export.request` and one `data_export.download` event per request and download, actor and target the Account, with no `before`, `after`, or reason.
- **Locks.** The request locks the Account row `FOR NO KEY UPDATE` (so two requests run in turn), checking it is active, before it deletes or creates an export row. The build holds the Account `FOR KEY SHARE` (`hold_active_account`), then the export row `FOR UPDATE`, and builds only a pending row, so a rerun writes nothing. The download locks the export row `FOR UPDATE` and deletes it. Account deletion takes the Account `FOR UPDATE` first and then deletes the export, so it never races a build.
- **Email.** OPEN's own templates (`exports/email/data_export_ready`), on the shared layout, with one action link, the expiry time in UTC, and no attachment.

## Consequences

- A player gets a complete, readable copy of their data, and a stolen session alone cannot pull it: the request needs the password or a code again, and the file needs the emailed key.
- The JSON is encrypted at rest and lives at most 24 hours after it is built. Every process that builds or downloads an export needs `FIELD_ENCRYPTION_KEYS`, as for any encrypted field, and `reencrypt_fields` rewrites the export too.
- The export is built in one task run and held in memory. An Account with a very long history makes a large file; if that becomes a problem, the build needs streaming or paging and its own record.
- Each request is an Audit Event with the Account as actor, so an Account that requested an export is kept as a cleared row at Account deletion, like one that ended a session (ADR 0048).
- A rating a replay removed no longer exists, so the events that targeted it are not found; its removal still shows in `rating_history`.
- The coverage test fails when a new model links to an Account or a Player, so the issue that adds it decides whether the export shows it.
- The web app needs a page at `/account/export/{key}` that signs the player in if needed and calls `download_data_export`.

## Alternatives considered

- Object storage with a signed URL: one more service and a link that works without sign-in.
- The key in a query string or a GET: it would reach logs and browser history, and a GET could be prefetched.
- Reauthentication on the download too: the emailed key and sign-in already bind it to the owner, and the player may open the email on another device later.
- A setting for the link lifetime: the maintainer fixed it at 24 hours.
