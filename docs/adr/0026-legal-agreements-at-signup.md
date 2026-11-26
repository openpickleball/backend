# 0026. Accepting the legal agreements at signup

- Status: Proposed
- Amended by: [ADR 0048](0048-account-deletion.md) (Account deletion deletes the acceptance rows)
- Date: 2026-11-24

## Context

The project owner wants signup to ask the player to accept OPEN's legal agreements with checkboxes (issue #222). The documents are now published on openpb.org: the Terms of Service, "Version 1, effective November 23, 2026", at openpb.org/terms/, and the Privacy Policy, "Version 2, effective November 24, 2026", at openpb.org/privacy/. Nothing recorded that a player accepted either one.

The owner decided on 2026-11-24 that signup requires accepting both, and that OPEN records per Account the agreement, its version number, and the time. The current versions live in settings. Asking existing players again after a version changes is out of scope here (#237). The owner also decided that the acceptance follows the rules of the other signup details (ADR 0017, ADR 0022): never held for a refused under-13 signup, the session changes the same way for a new and a taken address, and only a new Account gets a record.

## Decision

This amends ADR 0017 and ADR 0022 (signup checks and holds more), and ADR 0009 (a new record of personal data).

- Signup on both headless clients takes `accepted_terms` and `accepted_privacy`, both required (`SignupAgeForm`, so allauth's OpenAPI document lists them as booleans). Only JSON `true` accepts. A missing value, `false`, or anything else (a string such as `"true"`, a number, a list) gets allauth's 400 with a field error (`param` set) and a fixed message ("Accept the Terms of Service.", "Accept the Privacy Policy."). They are checked in the same step as the age and the other details, before allauth reads the email address. Like a malformed age, a refusal here sets no refusal flag and holds nothing.
- The current versions are whole numbers in settings: `TERMS_OF_SERVICE_VERSION` (1) and `PRIVACY_POLICY_VERSION` (2). Each can be set from the environment; empty means the version in `config/settings.py`. A value that is not a whole number from 1 stops startup, and the error names the variable without its value. The request says only that the player accepts; the version recorded is the one in settings when the request arrives.
- A signup that passes holds the accepted versions in `open_signup_details` with the other details, as `agreements` (`{"terms_of_service": 1, "privacy_policy": 2}`), bound to the new Account or to none for a taken address. The Player create endpoint does not read them.
- A new Account gets one `accounts.AgreementAcceptance` row per agreement: the Account, the agreement (`terms_of_service` or `privacy_policy`), the version, and the time accepted. The rows are written in the same transaction that creates the Account (`SignupInput.try_save`), so an Account never exists without them. A taken address creates no Account and no row. An under-13 signup is refused before any of this.
- The database refuses a version below 1, an agreement outside the two, and a second row for the same Account, agreement, and version. A later version gets its own row (#237), so the history of what an Account accepted is kept.
- The rows go with the Account (`CASCADE`). Account deletion deletes them, also on a kept Account row (ADR 0048).

## Consequences

- Every Account made through signup from now on has a record of the version of each agreement it accepted, and when. Accounts made before this change, and Accounts made by staff or `createsuperuser`, have none. #237 asks every Account that has not accepted the current versions to accept them.
- The web and Expo apps must send `accepted_terms: true` and `accepted_privacy: true` with every signup, after the player ticks the boxes that link to the published pages (openpickleball/frontend#197).
- Publishing a new version on openpb.org needs the matching setting changed in the same release, or signup records the old number. Until #237 lands, a change of version does not ask existing players again.
- The acceptance is personal data linked to an Account. The inventory lists it, and the data export (#39) must include it.

## Alternatives considered

- **Send the accepted versions in the request.** The app would send the version it showed, and the backend would refuse a stale one. It adds a failure the player cannot fix, and the app would need the versions from somewhere. Booleans keep the app simple; the backend knows the current versions.
- **Record the acceptance when the Player is created.** The Player create endpoint takes the other held details. But the agreements bind the Account, and an Account can exist without a Player, so the record belongs to the Account at signup.
- **Store the versions on the User row.** Two columns would hold only the latest acceptance and lose the time of each. One row per accepted version keeps the history #237 adds to.
- **Fix the versions in code only.** The owner asked for settings read from the environment, so an operator can change a version with the published page.
