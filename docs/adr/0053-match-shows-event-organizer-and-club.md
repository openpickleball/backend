# 0053. A match shows its Event, Organizer, and Club

- Status: Proposed
- Date: 2026-11-26

## Context

`MatchOut`, the match in every first-party match response, said nothing about the match's Event or Club, though the match stores both (issue #302). The web app needs them for two things. A player who thinks an Event's Organizer is at fault reports it (ADR 0037), and `report_organizer` takes the Organizer's OPEN Code, which the app could not learn. A player's correction (#267) is refused for a match with an Event or a Club, and the app could not tell which matches those are, so it could not hide the action.

Until now an Event's code and name, and an Organizer's code, went through the API only to that Organizer's own members (#125, #301, and the personal data inventory). The Organizer's name is an identifying field, since it may be a person's name (ADR 0009), and goes only to the Organizer's own members (ADR 0050). `MatchOut` reaches each participant, any signed-in Account a player's `match_history_visibility` allows for a confirmed match in that player's history, and members of the Event's verified Organizer who may act for it, through the confirm response, the Organizer's dispute list and settling responses, and the holds list and vouch response (ADR 0015, ADR 0034).

This amends ADR 0015: first-party match responses also show the match's Event, its Organizer's OPEN Code, and its Club.

## Decision

- `MatchOut` gains `event` and `club`, each null when the match has none.
- `event` holds the Event's OPEN Code, its name, and `organizer`, the Organizer's OPEN Code (the value `report_organizer` takes). It never holds the Organizer's name, which still goes only to the Organizer's own members (ADR 0050).
- `club` holds the match's own Club (`Match.club`): its OPEN Code and name. A Club is a place or group, not a person, so it is not personal data (`docs/privacy/personal-data-inventory.md`).
- The new fields add no viewer rule: whoever a response already shows the match to sees them, on every operation that returns `MatchOut`. `MatchOut` still never returns `rating_format` (ADR 0024) or a row id (ADR 0008).
- `with_match_details` loads the Event, its Organizer, and the Club with the match, so a page of matches takes the same number of queries whatever its size.

## Consequences

- The app can offer "Report this organizer" from an Event match, say who settles a dispute (the players or the Event's Organizer, ADR 0034), and hide a player's correction on a match with an Event or a Club.
- An Event's code and name, and its Organizer's code, now reach the match's participants and the Accounts that may read a player's match history. For an Organizer that is one person, its code points to that person, and a match history now shows which of that person's Events a player played in. Accepted: the code is what reporting needs, and the name stays with members.
- A self-reported match that an Organizer's exact copy confirmed keeps no Event (ADR 0013, ADR 0021), so it shows no Organizer. That is its own decision, not taken here.
- The partner API (#32) decides for itself what it shows; this record covers the first-party `MatchOut` only.

## Alternatives considered

- The Organizer's name on `MatchOut` too: shows a possible person's name to every Account that may read a player's history. Rejected by the maintainer on #302; a later issue can take it up.
- The Organizer's name only for participants: a response whose shape depends on the viewer, for a field the app's report flow does not need.
- A separate lookup from a match to its Organizer: a second request for every match page, with the same visibility question.
