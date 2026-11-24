# OPEN backend: domain language

The shared vocabulary for this repository. Use these terms in models, endpoints, fields, tests, and docs. Where a term comes from the reference engine, the engine name is given in `code` so the two stay aligned. If the engine renames something, update this file.

## People and accounts

**Player**:
A person who plays matches and has ratings. Every Player is created by one Account and belongs to it. Deleting the Account (#116) keeps the Player and its Matches. Once Ratings are published, the display name and current Ratings of a Player whose Account is active are always visible to every signed-in Account, and the Player cannot hide them. The current Ratings of such a Player aged 18 or older are also visible to anyone, with no sign-in (ADR 0031). Authenticated partner applications see them too, except for players aged 13 to 17 (ADR 0009). There are no unclaimed Player records, and no Submission creates a Player (ADR 0002). Engine: `Player`. Backend: `players.Player`.
_Avoid_: user, member, athlete.

**Account**:
A registered OPEN login. An Account creates its own Player, and has at most one. The engine allows a Player with no Account, but the backend never creates one. #116 decides what a deleted Account leaves behind (ADR 0002). Engine: `Player.account_user_id`, `RatingEngine.link_account`. Backend: `accounts.User`, keyed on email.
_Avoid_: profile (a profile is the view of a Player that others see behind authentication).

**Display Name**:
The name a Player shows to others. As ADR 0002 sets out, once Ratings are published, the Display Name of a Player whose Account is active is visible to every signed-in Account. Partners see it too, except for players aged 13 to 17 (ADR 0009). While a Player's Account is inactive, match responses show "Private player" in place of the Display Name and OPEN Code (ADR 0015). Signed-in Accounts can find a Player whose Account is active by the start of a word in their Display Name, before Ratings are published too (ADR 0018).
_Avoid_: username, nickname.

**Legal Name**:
A Player's full legal name, private, used only for identity checks and Organizer verification. Encrypted once #115 lands, and never returned to Partners (ADR 0009).
_Avoid_: real name.

**Age Group**:
A band derived from a Player's birth year and month, shown in their place. OPEN stores birth year and month, never the day, and applies age limits as if the player were born on the last day of the month (ADR 0009).
_Avoid_: age, birthdate.

**Privacy Setting**:
A Player's choice of who sees one of their other profile details (city and region, match history, rating history, stats, home club): every active signed-in Account, or only the player. While the player's own Account is inactive, nobody sees them. Display Name and current Ratings have no Privacy Setting (ADR 0002). Backend: the `*_visibility` fields of `players.PlayerPrivacy`.
_Avoid_: visibility flag, opt-out.

**Gender Division**:
The gender division a Player enters for divisions and gender-specific Rating Formats: men's or women's. Every new Player has one, chosen at signup, or when the Player is created if the signup session no longer holds it (ADR 0019). The player cannot change it after that (ADR 0023). OPEN stores no other gender data (ADR 0009).
_Avoid_: gender, sex.

**Organizer**:
A person or body that runs events and submits results on behalf of an event, Club, league, or tournament. An Organizer is unverified until an Account with permission to verify organizers verifies it. A verified Organizer's submission counts as confirmed. A person who organizes alone is an Organizer with one Organizer Member. Backend: `clubs.Organizer`.

**Organizer Member**:
An Account that acts for an Organizer, with a role: admin or match manager. While the Organizer is verified, a member with either role may submit matches for its Events (a match that also names a Club needs a Club Role there too) and change them. While an Organizer is verified, the Account that verified it cannot be its member. Backend: `clubs.OrganizerMember`.

**Club**:
A place or group where players play, with a name and an optional city and region. Backend: `clubs.Club`.
_Avoid_: venue, team.

**Club Role**:
An Account's role at a Club: admin or match manager. Either role may submit the Club's matches (a match that also names an Event needs authority for that Event too). Of the Club Roles, only admin may change them. Backend: `clubs.ClubRole`.

**Event**:
A tournament, league, or verified rec play that an Organizer runs, optionally at a Club, from a start date to an end date. Its kind is the Match Type its matches count as: `tournament`, `league`, or `rec_verified`. Self-reported matches have no Event. An Event has no weight of its own: a match's weight comes from its Match Type alone. Backend: `events.Event`.
_Avoid_: competition.

**Partner**:
An app, league platform, club, or tournament that integrates with OPEN through the APIs. Partners use OPEN for free.
_Avoid_: customer, client, vendor.

**Partner Application**:
What a Partner registers to reach OPEN's APIs through OAuth. It starts unapproved; a moderator approves production access (#29). It may belong to an Organizer. Once approved, it may submit matches for the Events of the verified Organizer it belongs to and for each Club that granted it. A match that names both an Event and a Club needs both. It never submits a match with no Event and no Club, and it may change only the matches it submitted. Backend: `partners.PartnerApplication`.
_Avoid_: client, app (alone), integration.

**Club Application Grant**:
A Club's permission for one Partner Application to submit that Club's matches, once the application is approved. Backend: `partners.ClubApplicationGrant`.
_Avoid_: authorization, consent (that is a player's approval of a linked app, #76).

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
Where the result came from, which affects its weight: `tournament`, `league`, `rec_verified`, or `self_reported`. Not stored: an Event match has its Event's kind, a Club match with no Event is `rec_verified`, and a match with neither is `self_reported` (ADR 0024). Engine: `MatchType`.

**Confirmation**:
Agreement that a reported score is correct. A self-reported Match stays pending until at least one player from each team confirms it (a player's own submission counts as their team's confirmation), or an Organizer, Club, or Partner Application with permission submits the same Match (same teams, date, and scores). A Match that an Organizer, Club, or Partner Application with permission submits is confirmed when it is created (decision F4). Only a confirmed Match can count. Engine: `Match.confirmed_by`. Backend: `matches.MatchConfirmation`, one row per player's decision, and `Match.status` (ADR 0013). For a self-reported Match a moderator confirmed, the engine gets every participant as confirmed, since the moderator's decision stands in for the teams (ADR 0024).

**Dispute**:
A participant's statement that a Match's score is wrong. A disputed Match stops counting until a moderator resolves it as confirmed or voided. Each player disputes a Match at most once. Backend: `matches.MatchConfirmation` with decision `dispute`.

**Correction**:
The new Match that replaces a wrong one. The wrong Match is voided, never edited, and the Correction points to it (`Match.corrects`), so the Ledger keeps both. A void with no replacement removes a Match from counting the same way.
_Avoid_: delete. The backend calls making a Correction an edit (`edit_match`, `match.edit`); it never changes the stored Match.

**Submission**:
A request to add a Match through the Results API. A Submission becomes a Match in the Ledger only after it passes the submitter's permission check, validation, duplicate checks, and Confirmation.
_Avoid_: upload (an upload is a batch of Submissions).

**Flagged Match**:
A Match held for review because it looks like an outlier, a possible duplicate, or otherwise suspicious. A possible duplicate flag holds both Matches from counting, the flagged one and the earlier one it may repeat, until moderator review resolves the flag or one of the two is voided. A moderator resolves a flag as approved (the result stands) or dismissed; a resolved flag holds nothing and stays on record. Backend: `matches.MatchFlag`, one row per flag, so review never edits the Match.

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
Rebuilding all Ratings from Seeds plus the full Ledger as of a date. Same Ledger, Seeds, config, and date always give the same result. Replays run on a published schedule and produce the authoritative Ratings. The backend replays the whole Ledger when what counts changes (a void, correction, dispute, hold, or release of a Match, or a Match a Live Update cannot apply), and quick successive changes share one Replay. A Player left with no counted Match in a Rating Format loses that Rating. Engine: `replay()`. Backend: `apps.ratings.services.replay_ratings`, asked for through `ratings.RatingReplayRequest` (ADR 0028).

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
One recorded sensitive action: who did it (an Account, a Partner Application, both, or nobody), what it changed (the target), the values before and after (for an identifying personal data field, only its name, ADR 0009), and a reason. Audit Events are append-only. They record moderation, manual edits, every Match status change (confirmation, dispute, void, expiry, and correction), account security changes, privacy changes, a player's own profile changes, and partner application requests and decisions; they are not part of the Ledger and never feed ratings. Backend: `audit.AuditEvent`.
_Avoid_: log entry (Django's admin `LogEntry` is a different thing), history.

## Governance terms

**Rating Spec**:
The published, versioned definition of how OPEN ratings are calculated. Only the governing board changes the official spec.

**Conformance**:
Meeting the board's published requirements for showing an OPEN rating. Only conforming tools may display one.
