# OPEN backend: domain language

The shared vocabulary for this repository. Use these terms in models, endpoints, fields, tests, and docs. Where a term comes from the reference engine, the engine name is given in `code` so the two stay aligned. If the engine renames something, update this file.

## People and accounts

**Player**:
A person who plays matches and has ratings. A Player record can exist and collect OPEN matches before that person registers. Engine: `Player`.
_Avoid_: user, member, athlete.

**Account**:
A registered OPEN login. An Account can claim an existing Player record ("linking"). Players and Accounts are separate so that results can be recorded for people who have not signed up. Engine: `Player.account_user_id`, `RatingEngine.link_account`. Backend: `accounts.User`, keyed on email.
_Avoid_: profile (a profile is the public view of a Player).

**Organizer**:
A verified person or body that submits results on behalf of an event, club, league, or tournament. An Organizer submission counts as confirmed.

**Partner**:
An app, league platform, club, or tournament that integrates with OPEN through the APIs. Partners use OPEN for free.
_Avoid_: customer, client, vendor.

**External ID**:
A Player's identifier in another system, keyed by source. Used for identity matching, never as the OPEN primary key. Engine: `Player.external_ids`.

## Matches and results

**Match**:
One completed contest between two teams, with one or more game scores. Singles teams have one player; doubles teams have two. Every Match has a `match_id` unique to its source, used for duplicate protection. Engine: `Match`.
_Avoid_: game (a game is one scored unit inside a Match), result.

**Game**:
One scored unit inside a Match, such as 11-8. Must be valid for the Match's Game Format. Engine: `Match.games`.

**Game Format**:
How each game is scored: side-out or rally scoring, points to win, win-by margin, and an optional cap. Recorded on every Match. Engine: `GameFormat`.

**Match Type**:
Where the result came from, which affects its weight: `tournament`, `league`, `rec_verified`, or `self_reported`. Engine: `MatchType`.

**Confirmation**:
Agreement that a reported score is correct. A self-reported Match needs at least one player from each team to confirm it, unless a verified Organizer submitted it. Engine: `Match.confirmed_by`.

**Submission**:
A request to add a Match through the Results API. A Submission becomes a Match in the Ledger only after it passes validation, duplicate checks, and Confirmation.
_Avoid_: upload (an upload is a batch of Submissions).

**Flagged Match**:
A Match held for Organizer review because it looks like an outlier, a possible duplicate, or otherwise suspicious.

**Ledger**:
The append-only, ordered record of every counted Match. Order is date, time, sequence, then `match_id`. The Ledger is the source of truth for ratings. Engine: `ledger_order`.
_Avoid_: history table, log.

## Ratings

**Rating**:
A number on the 2.0 to 8.0 scale for one Player in one Rating Format. Engine: `RatingState.rating`.

**Rating Format**:
The kind of play a rating covers: `singles`, `doubles`, `mixed_doubles`, `doubles_men`, `doubles_women`. Category formats also update the overall `doubles` rating. Engine: `RatingFormat`.

**RD (rating deviation)**:
How uncertain a Rating is. Grows with inactivity and shrinks with play. Engine: `RatingState.rd`.

**Reliability**:
A 0 to 100% score shown with every Rating, earned through OPEN matches, opponent and partner variety, and recent play. It cannot be chosen or converted from another system. Engine: `reliability()`.

**Seed**:
A Player's starting Rating before any OPEN matches, either provisional or imported from an External Rating Snapshot.

**External Rating Snapshot**:
A verified rating from a standard external rating system, with its source, date, and evidence. Used only as a Seed. Once OPEN matches exist, a new snapshot cannot overwrite the Rating. Engine: `ExternalRatingSnapshot`.
_Avoid_: naming the external system.

**Imported Reliability**:
The external system's reliability, shown separately and clearly labeled until OPEN-earned Reliability exceeds it.

**Replay**:
Rebuilding all Ratings from Seeds plus the full Ledger as of a date. Same Ledger, Seeds, config, and date always give the same result. Replays run on a published schedule and produce the authoritative Ratings. Engine: `replay()`.

**Replay Info**:
The provenance stored with every Replay: as-of date, config version, config fingerprint, and match counts. Engine: `ReplayInfo`.

**Live Update**:
A rating change applied when a Match arrives, between Replays. Live Updates are provisional and labeled with how many have occurred since the last Replay. Engine: `RatingEngine.record`, `RatingState.live_updates_since_replay`.

**Frozen**:
A player in a Match dated on or before their imported baseline. Their rating is used for the prediction but does not move. Reported on every match result. Engine: `MatchResult.frozen`.

**Clamped**:
A player whose change was cut short by the rating floor or ceiling. Engine: `MatchResult.clamped`.

**Shadow Mode**:
Running the engine on real results without publishing or relying on its Ratings. OPEN is in shadow mode until validation results are published and the governing board is seated.

## Records

**Audit Event**:
One recorded sensitive action: who did it (an Account, or nobody), what it changed (the target), the values before and after, and a reason. Audit Events are append-only. They record moderation, manual edits, account security changes, and partner application requests and decisions; they are not part of the Ledger and never feed ratings. Backend: `audit.AuditEvent`.
_Avoid_: log entry (Django's admin `LogEntry` is a different thing), history.

## Governance terms

**Rating Spec**:
The published, versioned definition of how OPEN ratings are calculated. Only the governing board changes the official spec.

**Conformance**:
Meeting the board's published requirements for showing an OPEN rating. Only conforming tools may display one.
