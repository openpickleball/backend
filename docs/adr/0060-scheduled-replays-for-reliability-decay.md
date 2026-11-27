# 0060. Scheduled replays for reliability decay

- Status: Proposed
- Date: 2026-11-27

This amends [ADR 0028](0028-rating-replays-after-match-changes.md) (a new replay reason, `scheduled`, a new caller of `request_replay()`, and a replay that no match change asked for) and [ADR 0058](0058-full-recompute-command.md) (something now asks for a replay after a recompute with an earlier `--as-of`).

## Context

Issue #52 asks for a scheduled job that lowers the reliability of players who have stopped playing. Decision Q5 in #1 says the backend runs decay on a schedule but calls the engine to compute it (hard rule 2).

The pinned engine (`v1.0.0`) has no "apply decay" call. It measures inactivity at a reference date: a summary inflates RD for the days since the rating was last updated, up to `rd_max`, and lowers reliability from that RD and the player's recent variety. The rating never drifts. Its notes say authoritative ratings come from scheduled replays on the published schedule, `Config.replay_interval_days` (7 in `v1`), and that a stale player loses reliability as replays move forward.

So the engine's decay is a replay as of a later date. ADR 0028 replays only while a request is pending, and ADR 0058 noted that nothing asked for one on its own. A player who stopped playing kept the RD and reliability of the last replay.

## Decision

- **Decay is the scheduled replay.** `apps.ratings.services.decay_reliability()` stores one replay request with the new reason `scheduled` through `request_replay()`, and the replay task (ADR 0028) does the rating work. The backend computes no rating value.
- **When it is due.** At least one rating is stored, and either no replay has run or the `as_of` of the most recently created `RatingReplay` (by `created_at`, then id) is at least `replay_interval_days` before `replay_as_of()`. It is the newest replay, not the latest `as_of`, because the stored ratings come from the newest replay. The interval comes from the loaded engine config, read through `engine_adapter.replay_interval_days()`, so the cadence is the engine's.
- **Safe to run twice.** It writes nothing when no rating is stored (`no_ratings`), when the replay is not due (`not_due`), or when a `scheduled` request is already pending (`already_requested`). Requests for other reasons do not block it; the replay takes every pending request anyway. A second run on the same day finds the replay it asked for and writes nothing.
- **The schedule.** The `apps.ratings.tasks.decay_reliability` task runs daily at 03:45 UTC from the beat entry `decay-reliability`, after the two match expiry tasks at 03:00 and 03:30, so a replay they ask for usually runs first and the decay then finds nothing due. A run missed for an hour is dropped (`expire_seconds`).
- **Token clean-up.** The same issue asks for the expired OAuth token clean-up. `apps.partners.tasks.clear_expired_tokens` runs django-oauth-toolkit's `clear_expired()` (what `cleartokens` runs) daily at 04:20 UTC on the `default` queue. It keeps expired access tokens that still have a refresh token, and never touches device grants. With `REFRESH_TOKEN_REUSE_PROTECTION` on and no refresh token lifetime set, revoked refresh tokens stay, so a reused token can still revoke its family.
- **Where the schedule lives.** The issue asks to register the schedule in a data migration. This repository keeps every periodic task in `CELERY_BEAT_SCHEDULE`, which django-celery-beat's `DatabaseScheduler` copies over the stored row at each beat start, so a row made by a migration would be overwritten anyway. The entries still land in django-celery-beat's tables and show in the admin.

## Consequences

- A due replay is a full replay that holds the snapshot row lock. It runs at most once per `replay_interval_days` unless match changes replay sooner. Live updates wait for it, as for every replay.
- A scheduled replay runs on a new UTC day, so, as ADR 0028 notes for the first replay of each day, it moves `calculated_as_of` for nearly every rated state. It writes one `replay` history row and one `rating.change` Audit Event for each stored rating whose values change, which on a new UTC day is nearly every one, active players included, about once a week. A rating a live update already took as of that day can come out unchanged, and then nothing is written for it.
- A recompute with an earlier `--as-of` (ADR 0058) makes the scheduled replay due sooner, since the most recently created replay's `as_of` is what counts.

## Alternatives considered

- A per-player decay write from the stored snapshot as of today. Rejected: `apply_rating()` refuses a replay change whose date is not its replay's, and a new history kind would let stored ratings disagree with any replay.
- Running the replay inside the decay task. Rejected: the replay task already runs it, with its own limits and error handling.
- A backend inactivity threshold setting. Rejected: the engine's `replay_interval_days` is the cadence, and the engine measures inactivity per player.
