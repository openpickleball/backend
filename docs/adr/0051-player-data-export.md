# 0051. Player data export

- Status: Proposed
- Amended by: [ADR 0054](0054-organizer-self-service.md) (memberships gain `verification_requested`; Organizer Invites and the verification request statement are not exported)
- Date: 2026-11-26

## Context

The charter says players can always export their data, and ADR 0009 says the export holds every field in the inventory that belongs to the player, and that the file is deleted when its download link expires or at Account deletion, whichever comes first. Issue #39 asks for `POST /me/export`, a task that builds a JSON file, an email with a short-lived, single-use download link that works only for the owner, no private data of other players, a documented format, and an export that contains every model that references the player.

The export holds the most sensitive data OPEN keeps in one place: the decrypted legal name, birth year and month, and gender division (ADR 0046), sessions with IP addresses (ADR 0047), and the Account's Audit Events. The maintainer decided five questions on the issue.

## Decision

This amends [ADR 0009](0009-personal-data.md) (how the export is stored, what it holds, and when it is deleted; it settles the inventory's "set by #39" for the export's encryption), [ADR 0030](0030-api-throttling.md) (the request's own throttle), [ADR 0048](0048-account-deletion.md) (Account deletion deletes the export and the device grants, and requesting or downloading an export is acting), and [ADR 0025](0025-rating-state-history-and-replay-provenance.md) (each `rating.change` event also names the player).

### The maintainer's decisions

1. **Storage.** A `DataExport` row (`apps/exports`) holds the JSON, encrypted with the field encryption keys (`EncryptedTextField`, ADR 0046). No object storage.
2. **Lifetime and limits.** The link lasts 24 hours. The first successful download deletes the row. A new request replaces an earlier export. A 15-minute beat entry (`sweep-data-exports`) deletes expired rows. The request has its own day-window throttle, `API_DATA_EXPORT_THROTTLE_PER_DAY` (default 3), its only throttle, like the Organizer report throttle (ADR 0037).
3. **The link.** The email links to `{FRONTEND_URL}/account/export/{key}`. That page calls `POST /api/v1/me/export/download` with the key in the body, behind sign-in and the Account throttle. The key is stored hashed and must belong to the signed-in Account. The request needs a recent reauthentication (ADR 0048's 403 `reauthentication_required`); the download does not.
4. **Audit Events.** The export holds the events that target the Account, its Player, its privacy row, its rating states, or its agreement rows, plus the events the Account performed. Each shows its action, time, target type, and the target's OPEN Code (null when it has none). `before` and `after` appear only when the target is one of those own records. The actor appears only as `you`, `staff`, `other`, or `system`. The reason never appears. Match status events come through the matches section, so an event targeting a Match appears only when the Account performed it, without values.
5. **Scope.** The account (email, joined, last login), its email addresses with verified and primary status, its sessions (IP address, user agent, created, last seen; never the key or the row id), its MFA methods (type and dates; never a secret or the `data` field), Organizer memberships as ADR 0050 shows them (code, name, verified, `verification_requested` since ADR 0054, role), Organizer Reports it filed (Organizer code, reason, created, closed; never the close reason or anything about the closer), vouches, Submission Batches and results files it submitted (codes and dates), Partner Applications it owns (client_id and name; never the secret), and its agreement rows. Plus the Player's profile with the decrypted legal name, birth year and month, and gender division (the player's own only), privacy settings, matches played, confirmations, ratings, and rating history. Other players appear only as match responses show them (`match_out` with the Account as viewer: OPEN Code and display name, or "Private player").

### Choices made within those decisions

- **Every field that belongs to the player.** ADR 0009 says the export holds every inventory field that belongs to the player. So it also holds:
  - the Account's `is_active`, `is_staff`, `is_superuser`, and the names of its groups and permissions;
  - its Club Roles (Club code and name, role) and the Matches it submitted (code and submitted time);
  - the OAuth tokens and grants issued to it, device grants included (kind, application `client_id`, scope, a grant's redirect URI, created, expires, and a refresh token's revoked time; a device grant records no created time, so it exports `created: null`; never a token, code, or secret);
  - each owned Partner Application's settings (owning Organizer's code, redirect URIs, allowed origins, client and grant type, `is_sandbox`, `approved_at`, and timestamps);
  - a Submission Batch's `expected_size`, and a results file's status, `error_code`, `location`, and `game_target`;
  - each rating's stored detail and versions (`rd`, `matches_played`, `last_played`, `calculated_as_of`, the live update count, and the version fields), with its reliability and `demonstration: true`, and each rating change's Match OPEN Code, `delta`, and the same detail;
  - for each match played, its match type, `location`, `played_at` with its offset, its labels, and the match it corrects. The corrected match's OPEN Code appears only when the player also played in it: a correction can change the teams, and the match read hides the old match from anyone else. Otherwise it is null.

  Sections that add fields to a public schema subclass it (`ExportMatchOut`, `ExportRatingOut`, `ExportRatingHistoryItemOut`), so the public responses do not change.
- **Deliberate omissions.** Each holds a secret, another person's data, or a row id, or the maintainer fixed its shape:
  - `Organizer.verified_at`: Organizer memberships keep the ADR 0050 shape (decision 5), whose `verified` says the same.
  - `PartnerApplication.client_secret` and `hash_client_secret` (a secret), and `approved_by` (another Account).
  - `mfa.Authenticator.data` (the TOTP secret and recovery code seed), `UserSession.session_key` and `data`, and `sessions.Session.session_data` (session secrets and allauth's internal state).
  - `User.password` (a hash).
  - `ResultsFile.content` and `report` (other players' names and cell values).
  - `Match.rating_format` (it reveals every player's gender division, ADR 0024), `fingerprint` and `sequence` (internal), and `external_id` (a Partner's own identifier, not the player's).
  - A correction's `corrects` when the player did not play in the corrected match (`get_match` hides that match from them).
  - `MatchVouch.flag` (it names the other Match of a flag, ADR 0011).
  - `RatingHistory.replay` (a row id), and every row id in Audit Event values.
  - An OAuth record's token, code, nonce, code challenge, and claims.
  - The admin log (staff history whose `object_repr` can name others; the Account's own admin changes are its `admin.*` events).
  - Organizer Invites the Account sent (ADR 0054: short-lived, deleted on accept or expiry, and they hold another person's address; the Account's own `organizer_invite.create` events are in its audit events), and the Verification Request statement (the Organizer's, not the player's, and shared by its admins).
- **Coverage test.** A test walks every installed model with a foreign key or one-to-one link to the Account model or `players.Player` and requires each to be exported or listed in an exclusion map with a reason: the admin log (staff history, covered by the Account's own `admin.*` events), `Organizer.verified_by` and `PartnerApplication.approved_by` (covered by the events the Account performed), `OrganizerInvite.invited_by` (ADR 0054, see the omissions), and `DataExport` itself.
- **No Player.** An Account with no Player still gets an export; the Player sections are null or empty.
- **The key.** `secrets.token_urlsafe(32)`, made by the build task and stored only as its SHA-256 hex digest (unique). It exists only in the queued email (deleted once sent) and the player's mailbox. It is never logged, put in a task argument, or recorded in an Audit Event. A key that is empty, longer than 128 characters, not ASCII (a lone surrogate included, which cannot be hashed), unknown, expired, used, not yet built, or another Account's, and an inactive Account, get one fixed 404. These checks are the service's, not the schema's, so such a key gets the same 404, not a 422.
- **Format.** A set of ninja Schemas (`DataExportOut` and its sections, reusing `MyPlayerOut` and `OrganizerMembershipOut`, and extending `MatchOut`, `RatingOut`, and `RatingHistoryItemOut`), so the OpenAPI document describes it, plus `docs/data-export.md`. It carries `format_version` `"1"` and `generated_at`. The download returns the stored bytes as `application/json` with `Content-Disposition: attachment; filename="open-data-export.json"` and `Cache-Control: no-store`.
- **No row ids.** `before` and `after` drop every key that holds a row id: keys named like one (`id`, `ids`, `pk`, `pks`, `*_id`, `*_ids`), a rating change's `replay`, and an admin change's `groups` and `user_permissions`. The API never returns a row id (ADR 0008).
- **Actor kinds.** `you` is the Account itself, `staff` another Account that is staff or a superuser when the export is built, `other` any other Account or a Partner Application, and `system` no actor.
- **Answers.** The request answers 202 with `requested_at`, and 404 when the Account was deleted while it ran. The lifetime is a constant (`LINK_LIFETIME`, 24 hours), not a setting.
- **What the day limit counts.** ninja checks throttles before the view, so `DataExportThrottle` asks allauth itself whether the session reauthenticated recently (`config.auth.recently_reauthenticated`). Only a request that did counts against `API_DATA_EXPORT_THROTTLE_PER_DAY`. One that did not, which the view refuses with `reauthentication_required`, counts against the Account throttle's windows instead. So the plain flow (request, 403, reauthenticate, request) spends one export request, stray requests cannot lock a player out for a day, and refusals are still limited. The operation keeps one throttle object (ADR 0030).
- **A new request replaces the old export.** Decision 2 means a request deletes the earlier export even when its email has already gone out, so that link then gets the 404. A request made while a build runs waits for it and then deletes it, so its email can arrive with a link that no longer works; the new request's email follows.
- **Lost and failing tasks.** The build task gets only the row id. A pending row whose task was lost is queued again by the sweep once it was last queued more than an hour ago (`REQUEUE_AFTER`, the pattern of the results file sweep), and a pending row older than the link lifetime is deleted. So a build that fails every time is tried again about hourly and deleted 24 hours after the request, with no email; the player can ask again.
- **Removed ratings.** A replay can delete a rating state (`_remove_rating`). To keep that state's events, each `rating.change` event's `after` now also records the player's OPEN Code (`player`), and the builder takes the player's states from the live rows plus the states those events name. A state's id belongs to one player for good, and every state starts with such an event, so no other player's events are picked up. Events recorded before this change name no player, so a state removed before then is not found; its removal still shows in `rating_history`.
- **Audit.** One `data_export.request` and one `data_export.download` event per request and download, actor and target the Account, with no `before`, `after`, or reason.
- **Locks.** The request locks the Account row `FOR NO KEY UPDATE` (so two requests run in turn), checking it is active, before it deletes or creates an export row. The build holds the Account `FOR KEY SHARE` (`hold_active_account`), then the export row `FOR UPDATE`, and builds only a pending row, so a rerun writes nothing. The download holds the Account `FOR KEY SHARE` first, checking it is active, then locks the export row `FOR UPDATE` and deletes it; without that, its Audit Event's actor key would lock the Account at commit, after the export row, and deadlock with Account deletion. Account deletion takes the Account `FOR UPDATE` first and then deletes the export, so it never races a build or a download.
- **Email.** OPEN's own templates (`exports/email/data_export_ready`), on the shared layout, with one action link, the expiry time in UTC, and no attachment.

## Consequences

- A player gets a complete, readable copy of their data, and a stolen session alone cannot pull it: the request needs the password or a code again, and the file needs the emailed key.
- The JSON is encrypted at rest and lives at most 24 hours after it is built. Every process that builds or downloads an export needs `FIELD_ENCRYPTION_KEYS`, as for any encrypted field, and `reencrypt_fields` rewrites the export too.
- The export is built in one task run and held in memory. An Account with a very long history makes a large file; if that becomes a problem, the build needs streaming or paging and its own record.
- Each request is an Audit Event with the Account as actor, so an Account that requested an export is kept as a cleared row at Account deletion, like one that ended a session (ADR 0048).
- A state a replay removed before the `player` key existed loses its events from the export (see Removed ratings).
- The coverage test fails when a new model links to an Account or a Player, so the issue that adds it decides whether the export shows it.
- The web app needs a page at `/account/export/{key}` that signs the player in if needed and calls `download_data_export`. Because the key is in that page's path, the page should send `Referrer-Policy: no-referrer` and load nothing from another origin.

## Alternatives considered

- Object storage with a signed URL: one more service and a link that works without sign-in.
- A GET download with the key in its URL: a GET can be prefetched, and would put the key in the API's access logs. The emailed link does carry the key in the web app page's path, so it can reach that page's history and logs like a query string would. The protection is not secrecy of the URL: the key works once, expires after 24 hours, and needs the signed-in owner, and the page sends it to the API only in a POST body.
- Reauthentication on the download too: the emailed key and sign-in already bind it to the owner, and the player may open the email on another device later.
- A setting for the link lifetime: the maintainer fixed it at 24 hours.
