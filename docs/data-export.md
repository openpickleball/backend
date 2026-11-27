# Player data export format

A player can download a copy of the data OPEN holds about their Account (issue #39, [ADR 0051](adr/0051-player-data-export.md)). This page describes the file. The OpenAPI document (`openapi/openapi.json`) describes the same format as `DataExportOut`, the response of `download_data_export`.

## How a player gets it

1. The player reauthenticates (allauth's `auth/reauthenticate` with the password, or `auth/2fa/reauthenticate` with a code), then calls `POST /api/v1/me/export`. The answer is 202 with `requested_at`. Without a recent reauthentication it is 403 `reauthentication_required`. A new request replaces an earlier export. An Account may ask a few times a day (`API_DATA_EXPORT_THROTTLE_PER_DAY`, default 3); past that the answer is 429. Only a request with a recent reauthentication counts: one refused for lack of it counts against the Account's general request limits instead.
2. A task builds the file and emails the Account's login address a link to `{FRONTEND_URL}/account/export/{key}`. The email has no attachment.
3. That web app page, signed in as the same Account, calls `POST /api/v1/me/export/download` with `{"key": "..."}`. The answer is the file, `open-data-export.json`, sent once with `Cache-Control: no-store`. The file is then deleted, so the key works once.

The link lasts 24 hours after the file is built. An unknown, used, expired, or not yet built key, a key that is not plain ASCII, and another Account's key, all get the same 404. A new request deletes the earlier export, so a link already mailed for it then gets the 404 too. If the build keeps failing, OPEN tries it again about hourly and gives up 24 hours after the request, with no email; ask again.

## The file

One JSON object. Times are ISO 8601 in UTC. Dates are `YYYY-MM-DD`. Records go by OPEN Code; the file holds no row id, session key, secret, token, or Audit Event reason.

| Key | What it holds |
| --- | --- |
| `format_version` | `"1"`. A change that is not backward compatible gets a new version. |
| `generated_at` | When the file was built. |
| `account` | `email`, `date_joined`, `last_login`, `is_active`, `is_staff`, `is_superuser`, `groups` (names), `permissions` (`app_label.codename`). |
| `email_addresses` | Each address on the Account: `email`, `verified`, `primary`. |
| `sessions` | Each signed-in session the session list records: `ip`, `user_agent`, `created_at`, `last_seen_at`. |
| `mfa_methods` | Each two-factor method: `type` (`totp` or `recovery_codes`), `created_at`, `last_used_at`. Never a secret. |
| `agreements` | Each legal agreement version accepted: `agreement`, `version`, `accepted_at`. |
| `profile` | The player's own profile as `GET /api/v1/me/player` returns it: `code`, `display_name`, `legal_name`, `birth_year`, `birth_month`, `gender_division`, `city`, `region`, and `privacy` (each visibility setting). Null for an Account with no Player. |
| `matches` | Each match the player played, oldest first, as match responses show it to the player (`MatchOut`): code, status, format, date, scoring, teams, games, the player's team and latest decision, each team's confirmation, the Event (its OPEN Code, name, and Organizer's OPEN Code, ADR 0053) and Club. Each also has `match_type`, `location`, `played_at` and `played_at_utc_offset` (minutes east of UTC), `event_label`, `bracket_label`, and `corrects` (the code of the match it corrects, or null). Other players appear only with their OPEN Code and display name, or as `"Private player"` with a null code when their Account is inactive. Never the match's rating format, which would reveal the players' gender divisions. |
| `confirmations` | Each confirm, dispute, or withdrawal the player made: `match` (code), `decision`, `created_at`. |
| `vouches` | Each vouch the Account gave for a held match: `match` (code), `created_at`. |
| `submitted_matches` | Each match the Account submitted: `match` (code), `submitted_at`. |
| `submission_batches` | Each upload or bulk request the Account submitted: `created_at`, `event` and `club` (codes or null), `matches` (codes), `expected_size`, and `results_file` (`code`, `status`, `error_code`, `location`, `game_target`, `created_at`, `processed_at`, or null). Never the file's contents or report. |
| `ratings` | The player's current ratings keyed by rating format, as the rating lookup shows them (`RatingOut`), each with its reliability and `demonstration: true`, plus the stored detail: `rd`, `matches_played`, `last_played`, `calculated_as_of`, `live_updates_since_recalculation`, `config_version`, `params_version` (null when the config is not a published parameter set), `parameter_fingerprint`, `engine_version`. Empty when there are none. |
| `rating_history` | Each change to the player's ratings, oldest first (`RatingHistoryItemOut`), each with its reliability and `demonstration: true`, plus `match` (the causing match's code, or null), `delta`, and the same stored detail. |
| `organizer_memberships` | Each Organizer the Account is a member of: `organizer` (code), `name`, `verified`, `role`. |
| `club_roles` | Each Club Role: `club` (code), `name`, `role`. |
| `organizer_reports` | Each Organizer Report the Account filed: `organizer` (code), `reason`, `created_at`, `closed_at`. Never the close reason or who closed it. |
| `partner_applications` | Each Partner Application the Account owns: `client_id`, `name`, `organizer` (code), `redirect_uris`, `post_logout_redirect_uris`, `allowed_origins`, `client_type`, `authorization_grant_type`, `skip_authorization`, `algorithm`, `is_sandbox`, `approved_at`, `created`, `updated`. Never the secret or who approved it. |
| `oauth` | Each OAuth token or grant issued to the Account: `kind` (`access_token`, `refresh_token`, `id_token`, `grant`, or `device_grant`), `application` (`client_id`), `scope`, `redirect_uri` (a grant's), `created`, `expires`, `revoked` (a refresh token's). Never the token or code. |
| `audit_events` | The Audit Events about the Account's own records or made by the Account, oldest first (below). |

### Audit events

Each entry has:

- `action`, such as `privacy.change` or `match.confirm`.
- `at`, when it was recorded.
- `target_type`, the target's model as `app.model`, such as `players.player`.
- `target_code`, the target's OPEN Code, or null when it has none (the Account, a rating, an agreement row) or no longer exists.
- `actor`: `you` (the Account), `staff` (a staff Account), `other` (another Account or a Partner Application), or `system` (no actor, such as a rating replay).
- `before` and `after`: the recorded values, only when the target is the Account, its Player, its privacy settings, one of its ratings, or one of its agreement rows. Row ids are removed. Null otherwise.

A rating a replay removed keeps its events: each rating change event names the player's OPEN Code. Events recorded before that was added name no player, so a rating removed before then has no events here; its removal still shows in `rating_history`.

The list holds the events that target those records, plus every event the Account made, such as confirming a match or reporting an Organizer. An event the Account made on another record shows no values. A match's status changes by others are in `matches`, not here.
