# 0037. Players report Organizers to OPEN Moderators

- Status: Proposed
- Date: 2026-11-25

## Context

ADR 0034 sends a dispute on an Event match to the Event's verified Organizer. That Organizer may be the party at fault: compromised, or acting in bad faith. Moderators no longer review single matches, but they still decide whether an Organizer stays verified (ADR 0021). They need a way to hear about an Organizer, and the data to decide. Issue #271 asks for both. The project owner settled the open details on #271 on 2026-11-25.

## Decision

This amends ADR 0029 (the `Moderator` group gains `clubs.view_organizerreport`, and moderators close reports from the admin) and ADR 0030 (reporting is the second operation with a throttle of its own, after player search).

- **Who reports what.** Any active Account with a Player may report a verified Organizer, by its OPEN Code, at `POST /api/v1/organizers/{code}/reports`. The report has one reason from a fixed list (`wrong_results`, `fake_matches`, `ignores_disputes`, `misuses_player_data`, `other`) and no free text, so no text another person wrote is stored or mailed. An unknown or malformed code, an unverified Organizer, and an Account with no Player all get the same 404 (ADR 0008, ADR 0015). An inactive Account is not signed in, so it gets 401.
- **One open report.** An Account has at most one open report per Organizer, held by a partial unique constraint; a repeat gets 409. Once it is closed, the Account can report again.
- **A throttle of its own.** Reports have one day window, `API_ORGANIZER_REPORT_THROTTLE_PER_DAY` (default 5), which replaces the Account throttle on that operation. Every signed-in request counts, a refused one included, since ninja checks throttles after sign-in and before it parses input or runs the view.
- **Closing.** A moderator closes a report. Here that means an active Account with `clubs.verify_organizer`, the permission that verifies Organizers, that is not a member of the Organizer. It closes it from the admin, with a typed reason. An unverify that changes the status closes every open report on that Organizer in the same transaction. Nothing else closes a report, and a closed report stays on record while the Account exists.
- **What moderators see.** The Organizer's admin page has a review section. It shows only to an Account with `clubs.verify_organizer` and `clubs.view_organizerreport` that is not a member of the Organizer. It lists the open reports (reason and date). It also counts, over the last `ORGANIZER_REVIEW_WINDOW_DAYS` days (default 90), the Audit Events of matches on the Organizer's Events: disputes opened, withdrawn, resolved as confirmed, resolved as voided, and expired. It counts signal flags from the flag rows on those matches. Signal flag reasons came later: `unusual_submission` (#206, ADR 0049), and #207 adds a rule to it. An Organizer's correction of a disputed match is not counted as a resolution.
- **The reporter stays hidden from the Organizer.** The reporter is stored on the report row and as the actor of its `organizer_report.create` Audit Event, and nowhere else. No API response or email names them. The report admin shows them only as their Player's OPEN Code, and leaves out every report on an Organizer the viewer is a member of. The Organizer admin hides the review section from such a viewer.
- **A report changes nothing by itself.** No match, flag, rating, or Organizer field. A moderator acts only through `unverify_organizer` and the bulk void (ADR 0032).

## Consequences

- An Organizer that was ever reported cannot be deleted: `OrganizerReport.organizer` uses `PROTECT`, so the record of reports stays.
- A report row links an Account to the Organizer it reported. It is personal data, kept while the Account exists (ADR 0009). The Audit Event's actor uses `PROTECT`, so a reporter's Account is never hard-deleted. Account deletion (#116) must delete the Account's report rows itself. The `organizer_report.create` Audit Event, which links the Account to the Organizer, stays (ADR 0007).
- A 201 or 409 tells the reporter the Organizer exists and is verified. That is all it tells them, at five tries a day by default, and each 201 stores an audited report (a 409 means an earlier one did).
- The generic Audit Event admin shows every event's actor to Accounts with `audit.view_auditevent`. No Moderator or Organizer role holds that permission.

## Alternatives considered

- Free-text reports. Rejected: text a person writes can name others and cannot be erased from the audit log.
- Reports that close themselves after the review window. Not chosen: the project owner chose closing by a moderator or by an unverify.
- Only players who played in the Organizer's Events may report. Not chosen: the project owner chose any player.
