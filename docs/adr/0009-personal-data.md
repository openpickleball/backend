# 0009. Personal data: what OPEN collects and how it is protected

- Status: Accepted
- Amended by: [ADR 0019](0019-gender-division-required-at-signup.md) (gender division required for every new Player), [ADR 0020](0020-signup-age-check.md) (the app asks for the email and password before the age, and checks the age before it takes a name or a location), [ADR 0022](0022-name-and-place-at-signup.md) (signup takes the first and last name, stored as the legal name and starting display name, and an optional city and region), [ADR 0023](0023-birth-and-division-set-once.md) (the birth year and month and the gender division are set once, when the Player is created), [ADR 0026](0026-legal-agreements-at-signup.md) (signup records the version of each legal agreement an Account accepted, and when), [ADR 0031](0031-rating-lookup-and-history.md) (the current Ratings of a player aged 13 to 17 start hidden behind a privacy setting the player cannot widen before 18)
- Date: 2026-11-14

## Context

OPEN never charges players, so it stores no payment data (issue #93). It still needs a name, an age for age divisions and the 13-and-older rule, a gender division, and a city. A full name, birthdate, gender, and city together are what identity thieves want.

These choices are hard to change once players exist. So they are fixed before the first Player migration (#19).

ADR 0002 already decided two things. There are no unclaimed Player records: every Player is created by its own Account. And there is no opt-out: once Ratings are published, the display name and current Ratings of an active Account's Player are visible to every signed-in Account.

## Decision

### Principle

- Collect a field only for a stated feature need.
- Every personal data field is listed in [`docs/privacy/personal-data-inventory.md`](../privacy/personal-data-inventory.md), with its purpose, who can see it, retention, and whether it is encrypted.
- A pull request that adds a personal data field updates the inventory in the same pull request.
- Adding a field this record says OPEN will not collect needs a new ADR.

### Fields for a Player

Only the player sets these, on their own Player. No Organizer, Partner, CSV upload, or moderator sets them. A moderator may only remove them (#114, #116).

- **Display name.** Required. Visible as ADR 0002 sets out: once Ratings are published, the display name of a Player whose Account is active is visible to every signed-in Account, players aged 13 to 17 included. Partners see it too, except for players aged 13 to 17 (below). Not encrypted: it is public within OPEN.
- **Legal name.** Optional. The player adds it when an identity check or Organizer verification needs it. Seen by the player, and by staff whose role needs it, with an Audit Event each time (#115). Encrypted at the field level (#115). Never returned by any API except to the player themselves (their own profile and data export, #39).
- **Birth year and birth month.** Required to create a Player. Never the day. Seen by the player, and by staff whose role needs it, with an Audit Event (#115). Encrypted (#115). Never shown to others and never returned to Partners. Others see an Age Group instead (below).
- **Gender division.** Men's, women's, or empty. Optional. Used for the men's and women's doubles Rating Formats (`doubles_men`, `doubles_women`) and for mixed doubles eligibility (`mixed_doubles`). There is no raw gender field. Who may enter which division is a governance and event rule, not a backend rule. A Rating in `doubles_men` or `doubles_women`, shown under ADR 0002, reveals the division. This is accepted. The field itself is encrypted (#115) and is not shown as a profile field.
- **City and region.** Optional. It helps signed-in players tell apart players with the same display name, and find players near them. No street address, postcode, or exact location. A profile detail under the player's privacy settings (#19). Not encrypted.

### Age

- **Age Group.** A band derived from birth year and month, shown instead of them wherever an age matters to others. The bands follow the age divisions events use. The issue that first shows one defines them. A band never reveals the birth year or month.
- **End-of-month rule.** Wherever an age limit decides something, treat the player as born on the last day of their birth month. Example with made-up values: birth month May 2013 counts as born 31 May 2013, so the player turns 13 on 31 May 2026. Any error leans toward the younger, more protected answer.

### Players must be 13 or older

- Every path that creates a Player applies the 13-and-older rule by the end-of-month rule. It refuses anyone under 13 and keeps nothing from the attempt (#114).
- Signup asks for birth year and month first, on a neutral screen that does not hint at the limit. The age the signup screen collects is the one stored on the Player. Until the Player exists, the signup age may be held only in the signup session, on the server. It is deleted when the Player is created or the session ends. #114 decides the mechanics.
- Under-13 players are out of scope. US COPPA would require verifiable parental consent, separate consent for partner sharing, parent review and deletion rights, and legal review. The email-only consent method does not apply, since OPEN shows ratings to every signed-in Account and shares them with Partners.
- Parent-managed child profiles may come in a later milestone, with legal counsel, through a new ADR.
- Organizer CSV uploads carry the Organizer's confirmation that every player is 13 or older (#71).
- A player found to be under 13 has their personal data removed by a moderator. Their Matches stay in the Ledger without it (#114).

### Players aged 13 to 17

- Their display name and current Ratings stay visible to signed-in Accounts (ADR 0002).
- Their other profile details (city and region, match history, rating history, stats, home club) start at the most restrictive privacy setting #19 offers. The player can widen them.
- They are left out of all partner sharing until they turn 18. Partner API responses, batch lookups, webhooks, and Log in with OPEN authorizations never carry their data (#32, #75, #83, #85).
- Matches a Partner submits that name them still count. Only the response withholds their identity and Rating (#32).
- At 18 (by the end-of-month rule) their privacy settings stay as they are. They only become eligible for partner sharing.

### Unknown age

- A live Player always has a birth year and month. A Player cannot be created without them.
- An Account with no Player (for example a staff Account made with `createsuperuser`) stores no age on the Account row and holds no Player data.
- ADR 0002 removed unclaimed players, so unknown ages from organizer uploads never arise.
- A Player loses its birth year and month only by removal: when its Account is deleted (#116), or when a moderator removes an under-13 player's personal data (#114). Such a Player is excluded from all partner sharing, permanently.
- When #114 or #116 removes a required field (display name, birth year and month), that issue decides how the Player is shown.

### Account data

- **Email address.** The login (ADR 0003). Seen by the player, and by staff whose role needs it. Never returned by any API except to the Account itself. Not encrypted, because login lookups and the case-insensitive unique constraint compare it (ADR 0003). Protected by access control, and by the rule that it never appears in logs or error reports (#65, #66).
- **Password.** Only Django's salted hash is stored.

### Identifying personal data fields

The identifying personal data fields are the email address, display name, legal name, birth year and month, gender division, city and region, an Organizer's name, which may be a person's (#20), External IDs (#49), and platform player IDs (#82). The set holds every field that names, ages, locates, or points to the person outside OPEN, since any of them in an old Audit Event would re-identify a deleted Player. Rules in this record that name this set apply only to the set. Account deletion is wider and removes all personal data (Retention). All personal data, identifying or not, is listed in the inventory. Audit Events may record the values of other personal data: Ratings and Matches (ADR 0007), permissions, privacy settings (#19's privacy change audit task), and Organizer verification (#20: when, and which Account verified).

### What OPEN does not collect

- Payment data, such as cards and billing addresses. OPEN never charges (charter).
- Street address, postcode, or exact location.
- Full birthdate (the day).
- Raw gender identity.
- Phone number, unless a later ADR adds it.

### Where personal data must not appear

- Logs, error reports, and traces (#65, #66).
- Celery task arguments and return values (`CLAUDE.md`, Settings).
- API error messages (`CLAUDE.md`, API).
- Fixtures, seed data, and factories, beyond made-up Faker data (hard rule 1).
- Audit Event `before`, `after`, and `reason` values, for the identifying personal data fields. An Audit Event about an identifying personal data field records the field's name, never its old or new value.
- Partner APIs, webhooks, and OpenAPI examples never include legal name, birth year, or birth month (#115). The seed command (#41) sets only made-up values for them.

### Retention

Personal data stays while the Account exists. There is no automatic deletion of active or inactive Accounts.

- A refused under-13 attempt to create a Player keeps nothing.
- Deleting an Account removes all of its personal data from the live database at once, and ends and deletes its sessions (#116), except what this list names. What stays, and why:
  - The Player, with its OPEN Code, Matches, and Ratings (the Ledger, ADR 0002, hard rule 3).
  - Audit Events, which are append-only (ADR 0007) and never hold an identifying field's value (the audit rule in this record).
  - For an Account that has acted, its row with the identifying fields cleared (`PROTECT`, ADR 0007), and the links that point to it, such as `clubs.Organizer.verified_by`.
  - None of these keeps a name, email, or age.
- Everything else the inventory lists for that Account or Player goes, with two exceptions below: uploaded results files age out within 30 days, and copies held by outside processors follow #35. Examples of what goes: home club, privacy settings, Organizer memberships and Club Roles, the name of an Organizer that is that person, External ID and external rating (#49), platform player IDs (#82), email addresses, MFA authenticators, and Log in with OPEN grants.
- Deleting platform player IDs at Account deletion settles the #82 half of ADR 0002's open question on whether new Matches can name a deleted Player. The OPEN Code half stays with #116.
- An uploaded CSV results file is deleted within 30 days of processing (#71). Its results live on in the Ledger. The 30 days let the Organizer review the validation report. Because the file is gone within 30 days, Account deletion does not scrub players from stored files.
- Copies held by outside processors, such as the email provider (#35), follow that provider's retention. #35 records it and keeps it as short as the provider allows.
- A player found to be under 13: a moderator removes their personal data the same way (#114).
- Backups age deleted personal data out within 30 days (#67).
- Logs (#66) and error reports (#65) hold no personal data and are kept 30 days.
- Audit Events are kept indefinitely (they are append-only, ADR 0007). They never hold the value of an identifying personal data field.
- Expired sessions are removed by a scheduled task (#116). Deleting an Account also deletes its sessions (#116).
- A data export file (#39) is deleted when its download link expires, or at Account deletion, whichever comes first.

## Consequences

- #19 adds the Player fields above, its privacy settings, and its OPEN Code, and no other personal data field. It drops nothing beyond what ADR 0002 dropped. `Player.__str__` returns the Player's OPEN Code (see the admin log rule below). Its privacy settings need a most-restrictive level for players aged 13 to 17.
- #114 builds the age screen, the 13-and-older refusal, the end-of-month rule, and the removal of an under-13 player's personal data. Its task "Players aged 13 to 17 start with profile visibility set to private" means the other profile details only (ADR 0002).
- #115 encrypts legal name, birth year and month, and gender division ("gender" in #115 means the gender division). It audits staff access, and adds the check that every personal data field is in the inventory. Its task "the seed command (#41) never includes these fields" now means the seed command sets only made-up values for them, since a Player needs a birth year and month.
- #116 deletes all personal data on Account deletion and runs the session cleanup. Its criterion "no personal data about the player remains" means all personal data except the "What stays" list and the exceptions in Retention (uploaded results files and outside processors' copies). It has no unclaimed records to age out (ADR 0002) and no inactivity deletion to run. `AuditEvent.actor_user` uses `PROTECT` (ADR 0007), so an Account that has acted is kept as a deactivated row with its identifying fields cleared. Any other Account row may be deleted. #116 decides the mechanics.
- #67 sets backup retention so deleted personal data is gone from backups within 30 days.
- #39's export includes every field in the inventory that belongs to the player.
- #71 deletes each raw uploaded CSV results file within 30 days of processing, so Account deletion needs no per-player scrubbing of files.
- Every planned personal data field goes in the inventory when it lands.
- No model's `__str__` returns an identifying field, so Django's admin log (`django.contrib.admin.models.LogEntry.object_repr`) and admin messages never hold one. For example, `Player.__str__` returns the OPEN Code.
- `accounts.User.__str__` returns the email address today (`self.email`). This is the known exception. #116 clears every admin log entry tied to the deleted Account: its own, and any for a record that belongs to it.
- allauth's `EmailAddress` returns the email address from `__str__`. #30 unregistered its admin, so the admin log never holds it.
- Partner integrations cannot look up or receive data about players aged 13 to 17 until they turn 18. Matches a Partner submits that name them still count (#32).
- The age bands are not fixed here.

## Alternatives considered

- **Full birthdate.** The day adds little for divisions or the age limits, and it is what makes a birthdate useful for identity fraud.
- **Birth year only.** Up to a year of error at the 13 and 18 limits.
- **Under-13 players with parental consent now.** COPPA consent, partner-sharing consent, and legal review are out of reach at launch.
- **A raw gender field plus a derived division.** More sensitive data to protect, for no feature need.
- **Treating an unknown age as 13 to 17, or as an adult.** Rejected in favor of requiring birth year and month before a Player exists, so a live Player never has an unknown age. A Player whose birth year and month were removed is excluded from all partner sharing, the most protected answer.
- **Deleting inactive Accounts after a fixed period.** More machinery, and a risk of surprising players. A player can delete their Account at any time.
- **Encrypting the email address.** Login lookups and the case-insensitive unique constraint compare it (ADR 0003).
