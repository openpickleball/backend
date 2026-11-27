# OPEN backend: domain language

The shared vocabulary for this repository. Use these terms in models, endpoints, fields, tests, and docs. Where a term comes from the reference engine, the engine name is given in `code` so the two stay aligned. If the engine renames something, update this file.

## People and accounts

**Player**:
A person who plays matches and has ratings. Every Player is created by one Account and belongs to it. Deleting the Account keeps a Player that played, with its OPEN Code and Matches and its personal data cleared, and no new Match can name it (ADR 0048). Once Ratings are published, the display name of a Player whose Account is active is always visible to every signed-in Account. So are their current Ratings, unless the Player was created aged 13 to 17 and has not widened that Privacy Setting at 18 or later (ADR 0031). Authenticated partner applications see them too, except for players aged 13 to 17 (ADR 0009). There are no unclaimed Player records, and no Submission creates a Player (ADR 0002). Engine: `Player`. Backend: `players.Player`.
_Avoid_: user, member, athlete.

**Account**:
A registered OPEN login. An Account creates its own Player, and has at most one. The engine allows a Player with no Account, but the backend never creates one. Deleting an Account removes its personal data: a Player in the Ledger stays with its OPEN Code and its personal data cleared, and an Account row that something points at stays deactivated, with a placeholder email, so the address can sign up again (ADR 0048). Engine: `Player.account_user_id`, `RatingEngine.link_account`. Backend: `accounts.User`, keyed on email.
_Avoid_: profile (a profile is the view of a Player that others see behind authentication).

**Display Name**:
The name a Player shows to others. As ADR 0002 sets out, once Ratings are published, the Display Name of a Player whose Account is active is visible to every signed-in Account. Partners see it too, except for players aged 13 to 17 (ADR 0009). While a Player's Account is inactive, match responses show "Private player" in place of the Display Name and OPEN Code (ADR 0015). Signed-in Accounts can find a Player whose Account is active by the start of a word in their Display Name, before Ratings are published too (ADR 0018).
_Avoid_: username, nickname.

**Legal Name**:
A Player's full legal name, private, used only for identity checks and Organizer verification. Encrypted (ADR 0046), and never returned to Partners (ADR 0009).
_Avoid_: real name.

**Age Group**:
A band derived from a Player's birth year and month, shown in their place. OPEN stores birth year and month, never the day, and applies age limits as if the player were born on the last day of the month (ADR 0009).
_Avoid_: age, birthdate.

**Privacy Setting**:
A Player's choice of who sees one of their other profile details (city and region, match history, rating history, stats, home club) or their current Ratings: every active signed-in Account, or only the player. While the player's own Account is inactive, nobody sees them. The current Ratings setting cannot be narrowed, and a player aged 13 to 17 cannot widen it (ADR 0031). Display Name has no Privacy Setting (ADR 0002). Backend: the `*_visibility` fields of `players.PlayerPrivacy`.
_Avoid_: visibility flag, opt-out.

**Gender Division**:
The gender division a Player enters for divisions and gender-specific Rating Formats: men's or women's. Every new Player has one, chosen at signup, or when the Player is created if the signup session no longer holds it (ADR 0019). The player cannot change it after that (ADR 0023). OPEN stores no other gender data (ADR 0009).
_Avoid_: gender, sex.

**Organizer**:
A person or body that runs events and submits their results. The verified Organizer is OPEN's only trusted submitter (ADR 0021): an Organizer is unverified until an Account with permission to verify organizers verifies it, and a verified Organizer's submission counts as confirmed. A club that runs its own leagues or ladders becomes, or works through, an Organizer. A person who organizes alone is an Organizer with one Organizer Member. An Account may create an Organizer itself and is then its admin member; it stays unverified until a moderator verifies it (ADR 0054). Backend: `clubs.Organizer`.

**Organizer Member**:
An Account that acts for an Organizer, with a role: admin or match manager. While the Organizer is verified, the member's Account has two-factor authentication (ADR 0043), and no New Member Hold applies, a member with either role may create, change, and delete its Events, and submit matches for its Events and change them; a match or Event that names a Club also needs that Club's Club Organizer Grant to the Organizer. This is the only way an Account submits matches other than its own (ADR 0021). An admin member makes and removes the Organizer's Organizer Application Grants. While an Organizer is verified, the Account that verified it cannot be its member. Backend: `clubs.OrganizerMember`.

**Organizer Invite**:
An admin member's invite of an email address to act for an Organizer, as admin or match manager (ADR 0054). It is emailed as a link that works once and expires; the invitee accepts while signed in with that address, verified, and with a Player. The answer to the admin never says whether the address has an Account. Backend: `clubs.OrganizerInvite`.

**New Member Hold**:
The wait after an Account joins a verified Organizer by accepting an Organizer Invite, before it may act for the Organizer (ADR 0057). Until it ends the member has no authority as a member or as an admin; the Organizer's other admins are told of the new member. A moderator may end it early. Not the hold of a Flagged Match, which keeps Matches from counting. Backend: `clubs.OrganizerMember.hold_ends_at`.
_Avoid_: invitation code, join request.

**Club Invite**:
A Club admin's invite of an email address to become an admin of the Club (ADR 0055). It follows the Organizer Invite rules, with the admin role only. Backend: `clubs.ClubInvite`.
_Avoid_: invitation code, join request.

**Verification Request**:
An admin member's request that OPEN Moderators verify an Organizer, with a short statement of what it runs (ADR 0054). It changes nothing by itself: a moderator verifies the Organizer, which closes the request, or closes it without verifying. An Organizer has at most one open request. Backend: `clubs.OrganizerVerificationRequest`.
_Avoid_: application, verification ticket.

**Organizer Report**:
A player's report of a verified Organizer to OPEN Moderators, with one reason from a fixed list and no free text (ADR 0037). An Account has at most one open report per Organizer. A moderator closes a report, or an unverify of the Organizer closes all of them. A report changes nothing by itself: moderators decide only whether the Organizer stays verified, and may void its matches in bulk. The Organizer never learns who reported it. Backend: `clubs.OrganizerReport`.

**Club**:
A place or group where players play, with a name and an optional city and region. A Club is never verified and is not a trusted submitter: it decides only which Organizers may name it, through Club Organizer Grants (ADR 0021). An Account with a verified login address creates a Club and becomes its admin (ADR 0055). Backend: `clubs.Club`.
_Avoid_: venue, team.

**Club Role**:
An Account's role at a Club. The only Club Role is admin (ADR 0021). A Club admin manages the Club's profile, its other admins (by Club Invite), and its Club Organizer Grants. A Club Role gives no authority to submit, confirm, or change a match. Backend: `clubs.ClubRole`.

**Event**:
A tournament, league, or verified rec play that an Organizer runs, optionally at a Club, from a start date to an end date. An Event names a Club only when that Club granted its Organizer a Club Organizer Grant (ADR 0021). Every trusted match has an Event. Its kind is the Match Type its matches are recorded with: `tournament`, `league`, or `rec_verified`. The kind may change later, which changes only matches recorded after it (a Correction keeps the type of the Match it corrects). The Organizer and the Club cannot change once the Event has a match (ADR 0035). Self-reported matches have no Event. An Event has no weight of its own: a match's weight comes from its Match Type alone. Backend: `events.Event`.
_Avoid_: competition.

**Partner**:
An app, league platform, club, or tournament that integrates with OPEN through the APIs. Partners use OPEN for free.
_Avoid_: customer, client, vendor.

**Partner Application**:
What a Partner registers to reach OPEN's APIs through OAuth. It starts unapproved; a moderator approves production access (#29). It may belong to an Organizer. Once approved, it may submit matches for the Events of each verified Organizer that owns it or granted it an Organizer Application Grant (ADR 0021). A match that also names a Club needs that Club's Club Organizer Grant to the Event's Organizer. It never submits a match with no Event, and it may change only the matches it submitted. Backend: `partners.PartnerApplication`.
_Avoid_: client, app (alone), integration.

**Organizer Application Grant**:
An Organizer's permission for one Partner Application to act for it: once the application is approved and while the Organizer is verified, the application may submit and confirm matches for the Organizer's Events, as an application the Organizer owns may. A platform that serves many Organizers holds one grant from each. It replaced the Club Application Grant (ADR 0021). Backend: `partners.OrganizerApplicationGrant`.
_Avoid_: authorization, Club Application Grant (removed), consent (that is a player's approval of a linked app, #76).

**Club Organizer Grant**:
A Club's consent for one Organizer to name it on its Events and matches (ADR 0021). It gives consent, not trust: the Organizer must still be verified. A Club that removes a grant stops new matches and Events under its name; matches already recorded stay in the Ledger. Backend: `clubs.ClubOrganizerGrant`.
_Avoid_: club authority.

**External ID**:
A Player's identifier in another system, keyed by source. Used for identity matching, never as the OPEN primary key. Not the same as a Match's `external_id` (see Match). Engine: `Player.external_ids`.

**OPEN Code**:
The public identifier OPEN issues to a Player, Club, Event, Organizer, or Match, such as `P-7K3M-QX9H`. A type prefix, random characters, and a check character. It never changes and is never issued again. APIs, exports, and URLs use it; row ids never appear in the API. An External ID comes from another system and is only used for matching; an OPEN Code is OPEN's own. Backend: `public_code`, `public_ids.PublicCodeModel` (ADR 0008).
_Avoid_: OPEN ID, player ID, row id.

## Matches and results

**Match**:
One completed contest between two teams, with one or more game scores. Singles teams have one player; doubles teams have two. The engine needs a `match_id` unique to its source, used for duplicate protection; the backend sends the Match's OPEN Code, so a Correction gets a new one (ADR 0024). A Partner Application's own identifier for a Match is the Match's `external_id`, unique per application among Matches that are not voided (a Correction keeps it); a Match an Account submitted has none. Engine: `Match`. Backend: `matches.Match`, with its players in `matches.MatchParticipant`.
_Avoid_: game (a game is one scored unit inside a Match), result.

**Game**:
One scored unit inside a Match, such as 11-8. Must be valid for the Match's Game Format. A Match has 1 to 5 Games. Engine: `Match.games`. Backend: `matches.Game`.

**Game Format**:
How each game is scored: side-out or rally scoring, points to win, win-by margin, and an optional cap. Recorded on every Match. The backend stores the scoring type (`scoring_type`, side-out by default) and points to win (`game_target`: 11, 15, or 21); every game in a Match uses the same ones (decision F15). Engine: `GameFormat`.

**Match Type**:
Where the result came from, which affects its weight: `tournament`, `league`, `rec_verified`, or `self_reported`. Stored on each Match when it is recorded and never changed (ADR 0035): an Event match has its Event's kind at that time (a Correction copies the type of the Match it corrects), a Club match with no Event is `rec_verified` (none can be submitted since ADR 0021, so only one already in the Ledger or its correction), and a match with neither is `self_reported` (ADR 0024). Engine: `MatchType`.

**Confirmation**:
Agreement that a reported score is correct. A self-reported Match stays pending until at least one player from each team confirms it (a player's own submission counts as their team's confirmation), or a submitter with event authority (a member, with two-factor authentication, of a verified Organizer, or a Partner Application acting for one) submits the same Match (same teams, date, and scores) on its own, not in a Submission Batch (ADR 0036). A Match such a submitter submits, always with an Event, is confirmed when it is created (decision F4, ADR 0021). A Club gives no confirmation. Only a confirmed Match can count. Engine: `Match.confirmed_by`. Backend: `matches.MatchConfirmation`, one row per player's decision, and `Match.status` (ADR 0013). Moderators do not confirm Matches as moderators (ADR 0034).

**Dispute**:
A participant's statement that a Match's score is wrong. A disputed Match stops counting until it is resolved or corrected, or every open dispute is withdrawn. The participant who disputed can withdraw the dispute. The Match stays disputed while any other dispute is open. After the last withdrawal it is confirmed again when it was confirmed before, and pending otherwise (ADR 0034). A withdrawn dispute still counts as that player's one dispute. For a self-reported Match, the players settle it: by withdrawal, or by a participant's Correction. For a Match with an Event, the Event's verified Organizer settles it (a member whose Player is not in the Match's correction chain or the correction, or the approved Partner Application that submitted the Match while it still acts for that Organizer), with a required reason (ADR 0034). Moderators do not resolve disputes (ADR 0034). A dispute nobody resolves within the dispute period (30 days by default, from the first dispute still open since the last resolution) voids the Match (ADR 0034). Each player disputes a Match at most once. Backend: `matches.MatchConfirmation` with decision `dispute`, and `withdraw` for a withdrawal.

**Correction**:
The new Match that replaces a wrong one. The wrong Match is voided, never edited, and the Correction points to it (`Match.corrects`), so the Ledger keeps both. A void with no replacement removes a Match from counting the same way. A participant can correct a pending or disputed self-reported Match, keeping the same two teams: the Correction is pending, with the corrector's confirmation, until a player on the other team confirms it. The correcting player is its submitter. A confirmed self-reported Match must be disputed first (ADR 0034).
_Avoid_: delete. The backend calls making a Correction an edit (`edit_match`, `match.edit`); it never changes the stored Match.

**Submission**:
A request to add a Match through the Results API. A Submission becomes a Match in the Ledger only after it passes the submitter's permission check, validation, duplicate checks, and Confirmation.
_Avoid_: upload (an upload is a batch of Submissions).

**Submission Batch**:
The Submissions one submitter sent together in one CSV upload or one bulk request. Each Match it created links to it, so a moderator can find the batch's Matches by that link and void them in one action. A Correction does not join its Match's batch. Backend: `matches.SubmissionBatch` and `Match.submission_batch` (ADR 0032).
_Avoid_: upload, import (as names for the record).

**Results File**:
A CSV results file an Organizer sent for one Event, and its report of every row: recorded, a duplicate, or failed, with the reason. Only the rows that pass are recorded, as one Submission Batch. A batch Submission never confirms a pending Match. The file itself is kept only until it is processed; the report stays. Backend: `matches.ResultsFile` (ADR 0036).
_Avoid_: upload, import (as names for the record).

**Flagged Match**:
A Match marked for review because it looks like a possible duplicate or an unusual submission. An unusual submission flag is a signal only: it holds nothing, and the Match counts as before. It marks a trusted submitter's Match that is among an Organizer's first matches since it was verified (or an application's first since it was approved), came in a large upload, or is past the submitter's daily number (ADR 0049), or takes one of its players past a daily number of matches on its date played (ADR 0052), and names the rule that fired. The Organizer will see these flags in its flag feed (#213); moderators see only their counts in the Organizer's review. A possible duplicate flag holds both Matches from counting, the flagged one and the earlier one it may repeat, until vouching resolves the flag or one of the two is voided. A possible duplicate flag still open after the hold period (14 days by default) voids the newer Match when the earlier one is confirmed. The newer Match is usually the flagged one, but a Correction keeps its original's place while its chain keeps the same teams and date: each side counts as recorded when the first Match of its own same-teams-and-date correction chain was, so a Correction of such a Match recorded before the other one is the earlier Match. A Correction that moved a Match onto other teams or another date ranks as recorded when it was made. That ends its hold on the earlier Match, which counts again unless another open flag still holds it (ADR 0034). A hold against a pending or disputed earlier Match never ends this way. The parties end a possible duplicate hold by vouching: each Match of the flag is vouched for at its own level, a Match with an Event by its Event's verified Organizer (the actors and conflict rule that settle a Dispute) and a self-reported Match by one player of each team. Once both Matches are vouched for, the flag is resolved as approved. A vouch covers only the flags open when it was made, and cannot be undone. A legacy Match with a Club and no Event has no vouching level (ADR 0034). Moderators do not resolve flags (ADR 0034). A resolved flag holds nothing and stays on record. Backend: `matches.MatchFlag`, one row per flag (with its `rule` for an unusual submission), so review never edits the Match, and `matches.MatchVouch`, one append-only row per vouch.

**Ledger**:
The append-only, ordered record of every counted Match. Order is date, time, sequence, then `match_id`. The Ledger is the source of truth for ratings. Engine: `ledger_order`. Backend: `played_on`, `played_at` with `played_at_utc_offset`, and `sequence` on `matches.Match`.
_Avoid_: history table, log.

## Ratings

**Rating**:
A number on the 2.0 to 8.0 scale for one Player in one Rating Format. Engine: `RatingState.rating`, `PlayerSummary.rating`. Backend: `ratings.RatingState`, one row per Player and Rating Format.

**Rating Format**:
The kind of play a rating covers: `singles`, `doubles`, `mixed_doubles`, `doubles_men`, `doubles_women`. Category formats also update the overall `doubles` rating. Engine: `RatingFormat`.

**RD (rating deviation)**:
How uncertain a Rating is. Grows with inactivity and shrinks with play. Engine: `RatingState.rd`. Backend: `rd` on `ratings.RatingState` and `ratings.RatingHistory`.

**Reliability**:
A 0 to 100% score shown with every Rating, earned through OPEN matches, opponent and partner variety, and recent play. It cannot be chosen or converted from another system. Engine: `reliability()`, `PlayerSummary.reliability`. Backend: `reliability` on `ratings.RatingState` and `ratings.RatingHistory`.

**Seed**:
A Player's starting Rating before any OPEN matches, either provisional or imported from an External Rating Snapshot.

**External Rating Snapshot**:
A verified rating from a standard external rating system, with its source, date, and evidence. Used only as a Seed. Once OPEN matches exist, a new snapshot cannot overwrite the Rating. Engine: `ExternalRatingSnapshot`.
_Avoid_: naming the external system.

**Imported Reliability**:
The external system's reliability, shown separately and clearly labeled until OPEN-earned Reliability exceeds it. Engine: `PlayerSummary.imported_reliability`. Backend: `imported_reliability`, stored apart from `reliability`.

**Replay**:
Rebuilding all Ratings from Seeds plus the full Ledger as of a date. Same Ledger, Seeds, config, and date always give the same result. Replays run on a published schedule and produce the authoritative Ratings. The backend replays the whole Ledger when what counts changes (a void, correction, dispute, hold, or release of a Match, or a Match a Live Update cannot apply), and quick successive changes share one Replay. A Player left with no counted Match in a Rating Format loses that Rating. Engine: `replay()`. Backend: `apps.ratings.services.replay_ratings`, asked for through `ratings.RatingReplayRequest` (ADR 0028). `manage.py recompute_ratings` runs the same Replay on demand, with a dry run (ADR 0058).

**Replay Info**:
The provenance stored with every Replay: as-of date, config version, config fingerprint, and match counts. Engine: `ReplayInfo`. Backend: `ratings.RatingReplay`, with the engine and spec versions.

**Live Update**:
A rating change applied when a Match arrives, between Replays. Live Updates are provisional and labeled with how many have occurred since the last Replay. Engine: `RatingEngine.record`, `RatingState.live_updates_since_replay`, `PlayerSummary.live_updates_since_recalculation`. Backend: `live_updates_since_recalculation` and the `provisional` property on `ratings.RatingState`; the `apps.ratings.tasks.update_ratings_for_match` task applies one when a Match is confirmed (ADR 0027). A Match played before a player's last match is late: it is not applied live and waits for a Replay.

**Engine Snapshot**:
The rating engine's whole state, saved after each Live Update that changes a Rating and after each Replay, so the next Live Update continues from it: every rated Player's state by OPEN Code, the processed match ids, and the history the engine's own rules need. One per parameter set. Engine: `RatingEngine.to_snapshot`, `RatingEngine.from_snapshot`. Backend: `ratings.EngineSnapshot` (ADR 0027).
_Avoid_: checkpoint (a Replay's saved state may use the same row, but the term is Engine Snapshot), cache.

**Rating History**:
The append-only record of every change to a Player's Rating in one Rating Format, with what caused it: a Live Update from one Match, or a Replay. A Replay that no longer produces a Rating removes it, with a `removed` row that keeps the removed values. Each other row holds the Rating's values after the change and the engine and spec versions that produced them. Backend: `ratings.RatingHistory`.
_Avoid_: audit log (each change also records an Audit Event, a different thing).

**Frozen**:
A player in a Match dated on or before their imported baseline. Their rating is used for the prediction but does not move. Reported on every match result. Engine: `MatchResult.frozen`.

**Clamped**:
A player whose change was cut short by the rating floor or ceiling. Engine: `MatchResult.clamped`.

**Shadow Mode**:
Running the engine on real results without publishing or relying on its Ratings. OPEN is in shadow mode until validation results are published and the governing board is seated.

## Records

**Audit Event**:
One recorded sensitive action: who did it (an Account, a Partner Application, both, or nobody), what it changed (the target), the values before and after (for an identifying personal data field, only its name, ADR 0009), and a reason. Audit Events are append-only. They record moderation, manual edits, every Match status change (confirmation, dispute, dispute withdrawal, void, expiry, and correction), account security changes, privacy changes, a player's own profile changes, Organizer verification, Event creates, changes, and deletes, changes to Organizer Members, Club Roles, and both kinds of grant, and partner application requests and decisions; they are not part of the Ledger and never feed ratings. Backend: `audit.AuditEvent`.
_Avoid_: log entry (Django's admin `LogEntry` is a different thing), history.

## Governance terms

**Rating Spec**:
The published, versioned definition of how OPEN ratings are calculated. Only the governing board changes the official spec.

**Conformance**:
Meeting the board's published requirements for showing an OPEN rating. Only conforming tools may display one.
