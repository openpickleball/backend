# 0052. Plausibility checks: Event dates and a player's daily matches

- Status: Proposed
- Date: 2026-11-26

## Context

A match with an Event comes from a trusted submitter and counts as soon as it is recorded (ADR 0021). ADR 0049 added signal-only flags for unusual submissions. Issue #207 asks for two more plausibility checks on the same submissions: a match dated outside its Event, and one player with an implausible number of matches on one day. The first is an error the submitter can fix. The second can be real (a long round robin), so it must not refuse or hold anything.

## Decision

- A match with an Event must be played within the Event's dates: `start_date <= played_on <= end_date`. `validate_match()` takes the Event's dates as plain inputs (`event_dates`) and does no database access. A date outside them is one issue on `played_on`, code `outside_event_dates`, with fixed text that names neither date. A `played_on` that already has an issue (`invalid`, `future_date`) gets no second one.
- `draft_match()` reads the Event's stored dates by id and passes them to `validate_match()`. Both callers, `record_match()` and `edit_match()`, run it after their permission checks, so a refused submitter or editor never learns anything from the dates, and nothing is read before the permission check. The dates are not read again under `hold_event()`'s share lock: an admin's date change that commits between the read and the lock is missed (accepted).
- The check applies to every path that drafts a match: a new submission (the API, the CSV upload, partner imports) and every `edit_match()` correction. An edit of an old match whose Event's dates changed later is refused until the dates are fixed. The CSV upload reports the issue on the row's `date` column; the parser is unchanged, since it does not know the Event.
- A new rule joins ADR 0049's set: `player_daily_matches`. A new match with an Event is flagged `unusual_submission` with this rule when, once it is recorded, any of its players has more than `UNUSUAL_SUBMISSION_PLAYER_DAILY_MATCHES` matches (default 30, a whole number from 1 to 1,000,000; a bad value stops startup and names the variable, never the value) with the match's `played_on`. So the match that takes a player past the number, and every later one, is flagged. One flag per match, however many players are past it.
- The count takes every match of the player that day that is not voided and corrects nothing: pending, confirmed, and disputed ones, self-reported ones included. Unlike ADR 0049's counts, voided matches do not count: the question is how many matches the player really played. Corrections do not count, as in ADR 0049, and an `edit_match()` correction is never flagged. A self-submission is never flagged.
- No new lock. The rule runs under the submitter's advisory lock with the other rules, which does not make a per-player count exact: two submitters recording one player's matches at once may each miss the other, which errs toward no flag.
- The flag is a signal only, as in ADR 0049: it never holds or refuses a match, stays out of `Match.objects.counted()`, changes no rating, and sends no `match_changed`, notice, or Audit Event. No API response shows it. The Organizer review counts it with the other signal flags.

## Consequences

- An Organizer that sets an Event's dates too narrow gets its matches refused with `outside_event_dates` and must fix the Event first. While no match names the Event, the Event API lets the dates change; after that only the admin can change them.
- Changing an Event's dates can make a later correction of an older match impossible until the dates cover it again.
- A real tournament day with more than 30 matches for one player gets flags that change nothing for players; the setting can be raised.
- A player's own self-reported matches can push a trusted match past the number. That is intended: the count is about the player's day, not the submitter.

## Alternatives considered

The maintainer chose the proposed default for each open question on #207. The alternatives were:

- Also flagging self-submissions (2b): needs a separate path in `record_match()`, and the Organizer flag feed (#213) and the Organizer review (#271) cover only Organizers' Events. A self-submission already needs the other team's confirmation.
- One advisory lock per player, in id order, after the submitter lock (3b): exact, but it adds locks after the lock ADR 0049 keeps last and needs a new deadlock analysis, for a signal that only errs toward no flag.
- Skipping the date check for corrections (4b): a correction could then move a match outside its Event.
- Checking the dates again under `hold_event()`'s share lock (5b): closes the race with an admin date change, but puts a validation error after a lock, against the rule that every validation and model rule runs before any lock, lookup, or write.
