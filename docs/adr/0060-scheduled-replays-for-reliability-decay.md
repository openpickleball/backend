# 0060. Scheduled replays for reliability decay

- Status: Proposed
- Date: 2026-11-27

This builds on [ADR 0028](0028-rating-replays-after-match-changes.md) and [ADR 0058](0058-full-recompute-command.md) and does not change them.

## Context

Issue #52 asks for a scheduled job that lowers the reliability of players who have stopped playing. Decision Q5 in #1 says the backend runs decay on a schedule but calls the engine to compute it (hard rule 2).

The pinned engine (`v1.0.0`) has no "apply decay" call. It measures inactivity at a reference date: a summary inflates RD for the days since the rating was last updated, up to `rd_max`, and lowers reliability from that RD and the player's recent variety. The rating never drifts. Its notes say authoritative ratings come from scheduled replays on the published schedule, `Config.replay_interval_days` (7 in `v1`), and that a stale player loses reliability as replays move forward.

So the engine's decay is a replay as of a later date. ADR 0028 replays only while a request is pending, and ADR 0058 notes that nothing asks for one on its own. A player who stopped playing kept the RD and reliability of the last replay.

## Decision

- **Decay is the scheduled replay.** `apps.ratings.services.decay_reliability()` stores one replay request with the new reason `scheduled` through `request_replay()`, and the replay task (ADR 0028) does the rating work. The backend computes no rating value.
- **When it is due.** At least one rating is stored, and either no replay has run or the latest replay's `as_of` is at least `replay_interval_days` before `replay_as_of()`. The interval comes from the loaded engine config, read through `engine_adapter.replay_interval_days()`, so the cadence is the engine's.
- **Safe to run twice.** It writes nothing when no rating is stored (`no_ratings`), when the replay is not due (`not_due`), or when a `scheduled` request is already pending (`already_requested`). Requests for other reasons do not block it; the replay takes every pending request anyway. A second run on the same day finds the replay it asked for and writes nothing.
- **The schedule.** The `apps.ratings.tasks.decay_reliability` task runs daily at 03:45 UTC from the beat entry `decay-reliability`, after the two match expiry tasks at 03:00 and 03:30, so a replay they ask for usually runs first and the decay then finds nothing due. A run missed for an hour is dropped (`expire_seconds`).
- **Token clean-up.** The same issue asks for the expired OAuth token clean-up. `apps.partners.tasks.clear_expired_tokens` runs django-oauth-toolkit's `clear_expired()` (what `cleartokens` runs) daily at 04:20 UTC on the `default` queue. With `REFRESH_TOKEN_REUSE_PROTECTION` on and no refresh token lifetime set, revoked refresh tokens stay, so a reused token can still revoke its family.
- **Where the schedule lives.** The issue asks to register the schedule in a data migration. This repository keeps every periodic task in `CELERY_BEAT_SCHEDULE`, which django-celery-beat's `DatabaseScheduler` copies over the stored row at each beat start, so a row made by a migration would be overwritten anyway. The entries still land in django-celery-beat's tables and show in the admin.

## Consequences

- A due replay is a full replay that holds the snapshot row lock. It runs at most once per `replay_interval_days` unless match changes replay sooner. Live updates wait for it, as for every replay.
- Every inactive player's RD changes at each scheduled replay, so each writes one `replay` history row and one `rating.change` Audit Event per rating format, about once a week.
- A recompute with an earlier `--as-of` (ADR 0058) makes the scheduled replay due sooner, since the latest replay's date is what counts.

## Alternatives considered

- A per-player decay write from the stored snapshot as of today. Rejected: `apply_rating()` refuses a replay change whose date is not its replay's, and a new history kind would let stored ratings disagree with any replay.
- Running the replay inside the decay task. Rejected: the replay task already runs it, with its own limits and error handling.
- A backend inactivity threshold setting. Rejected: the engine's `replay_interval_days` is the cadence, and the engine measures inactivity per player.
