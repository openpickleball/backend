# 0029. Moderator review in the admin

- Status: Proposed
- Amended by: [ADR 0034](0034-lowest-level-resolution.md) (moderators no longer review matches: disputes and holds are settled by players and Organizers or expire, and moderators keep organizer verification and the bulk void; #273 removed the approve, void, dismiss flags, and edit actions, `approve_match`, and `resolve_flags`, so nothing writes the `dismissed` flag resolution any more, and the admin now lists every match by default, with an optional disputed or held filter)
- Amended by: [ADR 0037](0037-organizer-reports.md) (the `Moderator` group also views Organizer Reports, and moderators close them from the admin)
- Date: 2026-11-24

## Context

Moderators review flagged and disputed matches and verify organizers (issue #40). A possible duplicate flag holds both of its matches from counting until review (ADR 0011), and a dispute stops a match counting until a moderator rules (ADR 0013). Since #47, a hold or a dispute also removes a counted match's effect from stored ratings at the next replay (ADR 0028), so review has to end a hold and send `match_changed` when it does. Score confirmation, organizer verification, and duplicate detection are security-sensitive areas (CLAUDE.md, hard rule 8). Later issues add flags that are signals only (#206, #207) and must never hold a match.

## Decision

- Review happens in the Django admin. The match admin is read-only and lists by default the matches that need review: a match that is not voided and is disputed, or has an open flag whose other match is not voided. Matches and players appear by OPEN Code only.
- A flag is resolved, never deleted. `MatchFlag` gains `resolution` (approved or dismissed) and `resolved_at`, both set or both empty. `Match.objects.counted()` ignores resolved flags. `apps.matches.workflow.resolve_flags` is the only code that resolves a flag. It takes a moderator and a required reason, resolves every open flag involving the match, records one `match.flag_resolve` Audit Event per flag, and sends `match_changed` with `change=release` for each match that counts again.
- Who resolved a flag is recorded only in its Audit Event, not on the flag, so the flag adds no link to an Account.
- The admin actions mean:
  - approve: the result stands (`approve_match`). Under the match's locks, a disputed match is resolved as confirmed, and its open flags are resolved as approved. A flag links two matches, so approving one side releases the other too. A pending match stays pending until it is confirmed.
  - void: a disputed match is resolved as voided (`resolve_dispute`), any other match is voided (`void_match`). Its flags stay open: a voided match no longer holds the other one (ADR 0013), and the review list leaves such a flag out.
  - dismiss flags: the open flags are resolved as dismissed, and the match's status is unchanged, so a dispute stays open. For a possible duplicate this ends the hold. A signal-only flag never enters `counted()`, so dismissing it changes nothing.
  - edit: one match, through `edit_match`.
  - verify and unverify an organizer, through `verify_organizer` and `unverify_organizer`.
- Every action requires a reason, typed in a box next to the action menu; the edit action takes its reason on the edit page instead. A blank reason, or one over 1000 characters, changes nothing. The reason is stored in the action's Audit Event. The match actions are shown only to moderators (`matches.moderate_match`), the organizer actions only to Accounts with `clubs.verify_organizer`, and each service checks the permission again.
- A moderator never acts as moderator on a match their own Player played in, or on any match of its correction chain, or on an edit that puts their Player in the new teams (`moderates` in `apps.matches.permissions`), as organizer verification refuses an Organizer's own members. `resolve_dispute`, `approve_match`, and `resolve_flags` refuse such a moderator, and the last two also refuse when the moderator played in the correction chain of a match an open flag links to the target. `void_match` and `edit_match` treat them like anyone else, so only event authority (ADR 0021) lets them change the match, and without a reason. The admin shows the same fixed refusal whichever rule failed.
- A `Moderator` group holds exactly the permissions review needs: `matches.moderate_match`, `matches.view_match`, `matches.view_matchflag`, `clubs.verify_organizer`, and `clubs.view_organizer`. A data migration creates it. A moderator Account also needs `is_staff` to sign in to the admin.

## Consequences

- A near duplicate no longer holds a real result forever: a moderator can release it, and the next replay restores its effect.
- Approving and dismissing a possible duplicate have the same effect on counting. They differ only in what the Audit Event and the flag record.
- A flag on a voided match stays open in the database. It holds nothing and is left out of the review list.
- Correcting a match through edit runs the duplicate checks again, so a correction that is still a near duplicate of the other match is flagged again and held until the next review.
- The services keep their signatures. `verify_organizer` and `void_match` still accept an empty reason from other callers; only the admin requires one.
- No manual rating edit exists, so `rating.edit` is still never recorded (hard rule 2).

## Alternatives considered

- Resolving a flag when either match is voided. Rejected: the void already ends the hold, and a second Audit Event for the same action adds nothing.
- Storing the resolving Account on the flag. Rejected: the Audit Event already records it, and another `PROTECT` link would make Account deletion (#116) harder.
- An intermediate confirmation page for each action. Rejected for now: the action form's reason box keeps every action one step, and the edit action already has its own page.
