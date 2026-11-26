# 0041. Players are told about matches a trusted submitter records

- Status: Proposed
- Date: 2026-11-25

## Context

A match a verified Organizer records for one of its Events counts at once, without the players' confirmation (ADR 0021). So does a match from an approved application acting for that Organizer. If the submitter is compromised or acts in bad faith, it can record many wrong matches, and the players would not know. The players are the best check: they know which matches they played, and any participant can dispute a match, which stops it counting (ADR 0013). Issue #208 asks that every player hear about each match recorded for them, and asks whether players can turn these notices off.

## Decision

- **Which matches.** A match with an Event that is created confirmed: a trusted submitter's new match, and a correction the Event's Organizer (or the application that submitted the match) makes, which is created confirmed. Also a pending self-submission that the Organizer's exact duplicate confirms (ADR 0013), since it then counts although the other team never confirmed it. A self-submitted match that stays pending and a player's correction send no such notice: they count only once the other team confirms them.
- **Who.** Each participant that `participant_player` allows to dispute the match: a player whose Account is active. The email lists each match's OPEN Code, date, scores from the player's side, and a link to `match_url()`, where they can dispute it. It says the dispute goes to the Event's Organizer, and that a player who thinks the Organizer is at fault can report it (ADR 0037). It names no player, Event, Club, or Organizer, and quotes no text another person wrote.
- **When.** After the change commits, from an `on_commit` callback registered in its transaction, so a change that rolls back sends nothing. The callback reads the match again and skips one that is no longer confirmed or disputed.
- **One email per player per batch.** A match recorded with a Submission Batch sends nothing by itself. The batch path sends one email per player for the whole batch, listing each of their matches in it that is confirmed or disputed by then. For a CSV upload that happens once, when its Results File stops processing: done, or failed as expired. When a bulk void stops the file, the bulk void sends the batch's notices after it has voided the matches, so only the matches it left counting are listed (one the moderator may not void, one recorded before a submitter void's date, or every match left when the void stops partway), and a batch it emptied sends nothing. The batch's emails are built in their own task (`send_batch_notices`, on the `default` queue), which gets only the batch's row id, so a large batch never runs out the time of the task that finished it. A single submission sends one email per match until #37's digest groups them.
- **No opt-out.** These notices are a security notice, like a password reset, so they are always sent and there is no setting to turn them off. Turning them off would remove the check this decision adds. A later issue can add a setting if players ask for one; it would need its own field and a privacy review (ADR 0009).

## Consequences

- An upload of 1000 matches sends one email to each player in it, not one per match. A large upload can still send many emails at once. #37's daily budget will spread them out.
- A player hears about an upload only once its processing ends, a few minutes for a large file. A file whose processing stops partway waits for a rerun or the three-day expiry before its players are told.
- A crash after the change commits and before the callback queues the emails loses those notices, as for the dispute and hold notices (#272), and so does a broker error when the batch task is queued (it is logged by the batch's row id). The outbox (ADR 0016) covers only what was queued. A batch task that is redelivered after a crash sends its notices again, and one that reaches its time limit stops and sends no more.
- Each confirmed correction emails the players again, so an Organizer that edits a match many times mails its players many times. The Account throttle (ADR 0030) bounds it, and #37's daily budget will too.
- A legacy club-only match (ADR 0021) sends no notice, since it has no Organizer to settle a dispute.

## Alternatives considered

- One email per match for uploads too. Rejected: a 50-match upload would send a player 50 emails.
- A per-player setting to turn the notices off, on by default. Rejected for now: the notice exists to catch fraud that a player who turned it off would miss.
- Notify when an upload starts. Rejected: the matches are not recorded yet, so the email could list nothing.
