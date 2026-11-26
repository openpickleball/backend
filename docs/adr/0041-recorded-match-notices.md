# 0041. Players are told about matches a trusted submitter records

- Status: Proposed
- Date: 2026-11-25

## Context

A match a verified Organizer records for one of its Events counts at once, without the players' confirmation (ADR 0021). So does a match from an approved application acting for that Organizer. If the submitter is compromised or acts in bad faith, it can record many wrong matches, and the players would not know. The players are the best check: they know which matches they played, and any participant can dispute a match, which stops it counting (ADR 0013). Issue #208 asks that every player hear about each match recorded for them, and asks whether players can turn these notices off.

## Decision

- **Which matches.** A match with an Event that is created confirmed: a trusted submitter's new match, and a correction the Event's Organizer (or the application that submitted the match) makes, which is created confirmed. A self-submitted match, a player's correction, and a pending self-submission that the Organizer's exact duplicate confirms send no such notice. Their players submitted them, and the confirmation request covers the rest.
- **Who.** Each participant that `participant_player` allows to dispute the match: a player whose Account is active. The email lists each match's OPEN Code, date, scores from the player's side, and a link to `match_url()`, where they can dispute it. It says the dispute goes to the Event's Organizer, and that a player who thinks the Organizer is at fault can report it (ADR 0037). It names no player, Event, Club, or Organizer, and quotes no text another person wrote.
- **When.** After the change commits, from an `on_commit` callback registered in its transaction, so a change that rolls back sends nothing. The callback reads the match again and skips one that is no longer confirmed or disputed.
- **One email per player per batch.** A match recorded with a Submission Batch sends nothing by itself. The batch path sends one email per player for the whole batch, listing each of their matches in it that is confirmed or disputed by then. For a CSV upload that happens once, when its Results File stops processing: done, or failed as expired. A file a bulk void stopped sends nothing, since its matches are being voided. A single submission sends one email per match until #37's digest groups them.
- **No opt-out.** These notices are a security notice, like a password reset, so they are always sent and there is no setting to turn them off. Turning them off would remove the check this decision adds. A later issue can add a setting if players ask for one; it would need its own field and a privacy review (ADR 0009).

## Consequences

- An upload of 1000 matches sends one email to each player in it, not one per match. A large upload can still send many emails at once. #37's daily budget will spread them out.
- A player hears about an upload only once its processing ends, a few minutes for a large file. A file whose processing stops partway waits for a rerun or the three-day expiry before its players are told.
- A crash after the change commits and before the callback queues the emails loses those notices, as for the dispute and hold notices (#272). The outbox (ADR 0016) covers only what was queued.
- A legacy club-only match (ADR 0021) sends no notice, since it has no Organizer to settle a dispute.

## Alternatives considered

- One email per match for uploads too. Rejected: a 50-match upload would send a player 50 emails.
- A per-player setting to turn the notices off, on by default. Rejected for now: the notice exists to catch fraud that a player who turned it off would miss.
- Notify when an upload starts. Rejected: the matches are not recorded yet, so the email could list nothing.
