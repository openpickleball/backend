# 0002. Player identity: no unclaimed players, and no opt-out

- Status: Proposed
- Date: 2026-11-14

## Context

Issue #1 left a question open (decision Q9): partner and CSV submissions may name people who have never signed up for OPEN. Its default was a short decision record (ADR) on unclaimed Player records, how a person claims one, and what opt-out means for matches with other players. The Player model (#19) depends on the answer. Issue #18 asks:

- How is an unclaimed Player record created and matched?
- How does a person claim a record, and how are false claims handled?
- What does opt-out mean?
- What is the minimum personal data stored for an unclaimed player?

Earlier decisions already narrowed this. Partners send OPEN Codes or their own player IDs that are mapped (#82) or linked through Log in with OPEN (#75). An unmapped ID fails that Submission, and partner submissions never create unclaimed players (decision F5). CSV uploads use OPEN Codes only for now. An unknown code fails that row, and no Player is found or created from a name (decision F13). That left organizer rosters as the only remaining source of unclaimed records.

The engine supports Player records with no Account (`Player.account_user_id` is optional, and `RatingEngine.link_account` links one later). The backend does not have to use that.

Unclaimed records bring hard problems. The person never agreed to have a record. Their age is unknown, so OPEN cannot tell whether they are a minor (#93). Someone else can claim the record falsely. Two records for one person must be merged, and the Ledger is append-only (hard rule 3), so a merge cannot rewrite past Matches.

Events and other players need to see every player's Rating, so a player cannot hide it.

## Decision

### No unclaimed Player records

- For now, OPEN has no unclaimed Player records. Every Player is created by one Account and belongs to it (#124). An Account has at most one Player.
- Nobody else creates a Player: not Partners, not CSV uploads, not Organizers, not moderators.
- So there is nothing to claim, no false-claim process, and no unclaimed personal data.
- Deleting an Account (#116) removes its personal data but keeps the Player and its Matches in the Ledger. #116 decides how, and how the Player is shown afterwards.

### How submissions name players

- Partner and bulk submissions never create a Player.
- They name a player only by OPEN Code (ADR 0008), by a confirmed platform mapping (#82), or by a Log in with OPEN link (#75).
- CSV uploads use OPEN Codes only for now (decision F13).
- A name never finds or creates a Player.
- An unknown or unmapped ID fails that Submission, or that CSV row.

### No opt-out

- Profiles are behind authentication, never open to anonymous visitors.
- Once Ratings are published, the display name and current Ratings (one per Rating Format) of every Player whose Account is active are always visible to every signed-in Account, minors included. A player cannot hide them, opt out, or pause.
- Authenticated partner applications see them too, except for players aged 13 to 17, whom #93 keeps out of partner sharing until 18.
- Privacy settings (#19) cover only other profile details, such as match history, rating history, stats, and home club.
- A player who wants out deletes their Account (#116).

### Personal data

No unclaimed players exist, so there is no minimum data set for one. Personal data for Players is set by #93.

## Consequences

- A league needs every player to sign up and create a Player before its results are uploaded. #124 already says this.
- #124's out-of-scope items "Claiming an existing unclaimed record" and "Organizer-created players from a roster" are settled. Neither is planned.
- #19 drops its "claimed status" task, its criterion "A player can exist without a user (unclaimed)", `opted_out_at`, its criterion "Opting out is recorded with a timestamp and an audit event", and the opt-out part of its audit task.
- #19's "private" visibility option no longer applies to a player's name and Ratings. It stays for the other profile details.
- With no anonymous access, #19's "public" visibility level and its "whether rating history is public" setting, and #93's "can choose to make them public", become the signed-in level.
- #93's "Unclaimed players" rule becomes: only the player sets birth year, birth month, gender, and legal name, on their own Player.
- #93's question about unknown ages for unclaimed players does not arise.
- #93's "private by default" rule for players aged 13 to 17 applies only to the other profile details. Their name and current Ratings stay visible to signed-in Accounts.
- The "Private player" label in #31 and #56 no longer applies to a player's name. Those issues need rewording. ADR 0015 keeps the label only for a Player whose Account is inactive.
- Mentions of opted-out players in #19, #53, #83, #85, #96, and #124 no longer apply. #124's "Private and opted-out profiles return 404 to the public" no longer applies: no profile can hide its name and Ratings, and none is opted out.
- #116's retention for unclaimed records has nothing to act on.
- An open question for #116: can new Matches still name a Player whose Account was deleted, through its OPEN Code or a #82 mapping? This record does not decide it.
- Issues that assume anonymous reads need authentication or a separate decision: #124's public profile endpoint, #53's rating lookups, and the embeddable rating card (#96, #121). This record does not decide them.
- Adding unclaimed records later needs a new record that supersedes this one in part. It must first decide how records are created and matched, how a person claims one, how false claims and merges are handled, and the minimum data stored. It must also bring back a way for people named without their consent to refuse being rated. That is where opt-out belongs.

## Alternatives considered

- **Organizer-created unclaimed records with claim codes.** An Organizer could add a roster and hand each person a code to claim their record. This brings consent, unknown ages, false claims, and merges, with no answer yet for any of them.
- **Claim by request and approval.** A person asks for a record and a moderator approves. It has the same problems, plus moderator load.
- **Partner or CSV creation from names.** Already refused by decisions F5 and F13. Names cause mismatches.
- **Opt-out as a pause that refuses new Matches.** It is complex, its errors leak the pause to submitters, and it hides Ratings that events need.
- **A private profile setting that hides name and Ratings.** Events could not see Ratings.
- **Deleting Matches when a player leaves.** This breaks the append-only Ledger (hard rule 3) and changes other players' Ratings.
