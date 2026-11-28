# 0064. Vetting Organizers and Partner Applications

- Status: Proposed
- Date: 2026-11-28

This amends [ADR 0010](0010-partner-application-model.md) (approval review rules), [ADR 0013](0013-match-confirmation-and-changes.md) and [ADR 0021](0021-organizers-are-the-trusted-submitters.md) (a new Organizer's matches are created pending during its trial), [ADR 0041](0041-recorded-match-notices.md) (a trial match's notice asks the players to confirm it), [ADR 0049](0049-flag-unusual-submissions.md) (the trial is the hold that the `first_matches` rule does not apply), and [ADR 0054](0054-organizer-self-service.md) (the Verification Request becomes a structured form).

## Context

A verified Organizer is OPEN's only trusted submitter (ADR 0021). Its matches count as soon as they are recorded, whether a member uploads a CSV file (ADR 0036) or an approved Partner Application submits them for it. So who gets verified decides who may move ratings (hard rule 8).

Today an Organizer's admin asks for verification with one free-text statement (ADR 0054), and a moderator decides from that. A newly verified Organizer's matches count at once. If the verification was wrong, the Organizer can record fake results between real players, whose OPEN player IDs are easy to find. The players get a notice (ADR 0041) and can dispute, and a moderator can void the matches in bulk, but only after the ratings moved. The `first_matches` rule (ADR 0049) sees a new Organizer's first matches and holds nothing, because a flag that holds would be a penalty with no appeal.

Partner Application approval (#29) has no written review rules yet. Some applications belong to one Organizer. Others are platforms that serve many Organizers through Organizer Application Grants (#231).

## Decision

### Two gates

- **Organizer verification** asks whether this is a real group that runs real events, and who answers for its results. Every Organizer passes it before it may record a trusted match, whether it uploads CSV files, uses a platform, or builds its own integration.
- **Partner Application approval** (#29) is a technical review of software that acts for verified Organizers. It never replaces Organizer verification.
- A platform serving many Organizers has no submission authority of its own. It submits only for verified Organizers that granted it (ADR 0021, #231). Each of those Organizers passes the first gate.

### Organizer verification (#372)

- The Verification Request becomes a structured form: a public website or social page, one or more public event listings, expected events and matches a month, how the Organizer will submit, the Clubs it runs events for, a short note, and attestations (honest results, no players under 13, OPEN may void its results and unverify it).
- The form never asks for a phone number, a street address, an ID document, or payment (ADR 0009, the charter). OPEN uses no third-party identity checks.
- Two evidence levels. **Strong:** the Organizer proves it controls a domain or website, and gives a public event listing. **Basic:** a public event listing and Club grants only. A moderator may verify on either. Basic evidence gets a longer trial, because the trial, not the form, is what protects ratings.
- A moderator verifies, with a reason, and is never a member of the Organizer (already the rule). The verification records which evidence level the moderator accepted.

### Automated checks (#373)

- A program gathers evidence for the moderator: domain or website control through a token published as a DNS TXT record or a file under `/.well-known/`, whether each event listing answers and is on the Organizer's domain, earlier Organizers with the same domain or members that were refused, unverified, or reported, and which named Clubs have granted the Organizer.
- Stale requests close after a set number of days. A verified Organizer goes back into the review queue when it adds an admin member or its unusual submission flags pass a threshold.
- No check verifies, refuses, unverifies, or closes a request for a reason of its own judgment. A person makes every approve, reject, and revoke decision, with a reason and an Audit Event.

### A trial for every new Organizer (#371)

- While an Organizer is in its trial, a match recorded for one of its Events is created pending, not confirmed. It counts once at least one player on each team confirms it. The Organizer and its applications cannot confirm it alone, and an exact duplicate from them does not confirm it.
- A moderator may release a trial Organizer's batch, or its matches since a date, with a reason and an Audit Event. The `moderates` rule applies: a moderator never releases a match they played in. This covers the time before launch, when few players will confirm.
- The trial ends when the Organizer has 25 matches confirmed by players or released and 30 days have passed since its current verification, with no open dispute and no open duplicate hold on its matches. Both numbers are settings. Unverifying and verifying again starts a new trial.
- The trial applies to every new Organizer in the same way. It is not a penalty for something a flag found, so it needs no appeal, and it does not change ADR 0049's rule that a flag holds nothing.
- A trial match's notice asks the players to confirm it, in place of the ADR 0041 notice that a match was recorded.

### Partner Application approval (#29, #74)

- Approval stays a moderator action (ADR 0010), with a reason and an Audit Event. The approver is never a member of the application's owning Organizer.
- An application is eligible for approval only with an owning verified Organizer, or as a platform acting only through grants, with HTTPS redirect URIs, scopes limited to those it declared, a security contact address, and acceptance of the partner sharing rules.
- It must first pass a scripted sandbox check (#74): submit matches, handle a 409 and a 429, and refresh its token. A pass makes it eligible. It never approves it.
- A platform serving many Organizers needs approval by two different moderators.

## Consequences

- A wrongly verified Organizer cannot move ratings with fake matches until players on both teams confirm them or a moderator releases them.
- A new Organizer's results count later than today, and before launch a moderator must release most of them by hand.
- Moderators review structured evidence that a program has already checked, so reviews are faster and more alike.
- A grassroots Organizer with no domain can still be verified. It waits longer in its trial.

## Alternatives considered

- **One form for Organizers and applications.** Rejected: they answer different questions. A platform runs no events, and an Organizer's own integration is still only as trusted as the Organizer.
- **Hold matches when the `first_matches` flag fires.** Rejected: a flag that holds is a penalty with no appeal (ADR 0049). A trial that applies to every new Organizer is not.
- **A trial released only by moderators.** Rejected: it does not scale after launch, and players are the best check on which matches they played (ADR 0041).
- **Automatic verification when every check passes.** Rejected: verification decides who moves ratings (hard rule 8), so a person decides.
- **Third-party identity checks.** Rejected: they collect personal data ADR 0009 does not allow, and they usually cost money.
- **Maintainers, not moderators, approve applications.** Not adopted: ADR 0010 and #29 already give approval to moderators, and OPEN has no maintainer role in the code. The two-moderator rule for platforms covers the larger risk.
