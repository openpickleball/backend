# Incident response plan

This plan says what OPEN does when a security problem is reported or found in the hosted service: who decides, what to do first, how players and partners hear about it, and how fast. The organization [security policy](https://github.com/openpickleball/.github/blob/main/SECURITY.md) says how to report a problem. This plan starts where that policy ends.

Keep it short. In an incident, read it top to bottom and do the steps in order.

## What counts as an incident

Any of these, confirmed or strongly suspected:

- **Personal data exposed.** Anyone saw, copied, or could reach personal data they should not have: an Account's email, a Player's legal name, birth year and month, gender division, or city and region, a data export, or anything else listed in `docs/privacy/personal-data-inventory.md`.
- **Unauthorized rating or match changes.** A match was recorded, confirmed, disputed, corrected, or voided by someone without the right to do it, or a rating changed through any path other than the ledger and a replay. This includes bypasses of score confirmation, organizer verification, duplicate detection, or identity linking.
- **Leaked credentials or tokens.** A secret left its store: `DJANGO_SECRET_KEY`, a `FIELD_ENCRYPTION_KEYS` key, database or Valkey credentials, `RESEND_API_KEY`, a partner client secret, an OAuth access or refresh token, a session token, or a staff Account's password or MFA secret.
- **Service compromise.** Someone ran code, read the database, or changed the deployment without permission, on App Platform, in GitHub, or in a dependency.

A bug that could have led to one of these, with no sign that anyone used it, is a vulnerability, not an incident. Fix it through the security policy's private reporting, and use the first steps below only as far as they help.

## Roles

| Role | Who | Backup |
| --- | --- | --- |
| Incident lead: runs the response, assigns the work, keeps the timeline | The interim steward ([governance](https://github.com/openpickleball/.github/blob/main/GOVERNANCE.md)), until the board is seated | **Not yet named.** Open item: the steward names a backup before launch |
| Disclosure decision: what is said, to whom, and when | The interim steward. Once the board is seated, the board, which may delegate it to the lead | The backup lead |

While the steward is the only administrator, the backup cannot act on infrastructure without access. Naming the backup includes giving them the access the first steps below need (App Platform, GitHub organization owner, the Resend account) and MFA on each.

If the lead cannot be reached within 4 hours of a confirmed incident, the backup leads. Whoever leads says so in the private incident record (see "Evidence") so there is never doubt about who is in charge.

## First steps

Do these in order. Write down every action, with the time in UTC, as you go.

### 1. Contain

Stop the harm first, even before the cause is clear.

- **Revoke tokens.** End the affected Accounts' sessions and app tokens: setting an Account's password in the admin ends all of its sessions (ADR 0047), and turning off `is_active` makes Django refuse every session it has. Delete a partner's OAuth access and refresh tokens in the admin, but note that this alone does not stop a partner whose client secret leaked: partners use client credentials, so the secret gets a new token at once (see "Disable affected applications").
- **Rotate secrets.** Replace any secret that may have leaked, in the App Platform console:
  - `DJANGO_SECRET_KEY`: it signs emailed verification and reset links, the session auth hash, and the remembered sign-in devices, so rotating it voids pending links, signs everyone out, and sends every returning player a new sign-in email at their next login. Say so in the player notice.
  - A `FIELD_ENCRYPTION_KEYS` key: put a new key first, run `manage.py reencrypt_fields`, then remove the leaked key (ADR 0046).
  - Database and Valkey credentials, and `RESEND_API_KEY`.
- **Disable affected applications or webhooks.** A partner client secret is stored hashed in the database, and the admin cannot replace a saved one. OPEN has no switch to turn a partner application off yet (application revocation is #29). For a partner application that is misused or whose secret leaked: if it has submitted nothing, delete it in the admin, which removes its tokens, and register a new one with a new secret once the partner is ready. If it has submitted matches, batches, or vouches, those records protect it from deletion: delete its tokens, and take the service down if it keeps getting new ones, until #29 gives a way to revoke it. When signed webhook delivery lands (#86), pause the affected endpoints too.
- **Stop bad submissions.** A moderator can void a submission batch, or every match one submitter recorded on or after a given day (UTC), in the match admin (ADR 0032). Organizer verification can be removed in the admin, with a reason.
- **Take the service down** (scale to zero, or roll back the deploy) only if nothing narrower stops the harm. A short outage is better than continued exposure.

### 2. Preserve evidence

Before deleting or rebuilding anything, keep what shows what happened.

- **Audit log (#17).** `audit.AuditEvent` rows are append-only and cannot be changed or deleted. They record admin actions, match and rating changes, MFA changes, and access to personal data. Save the rows for the incident window, with a read-only query on the `audit_auditevent` table or from the admin's audit event list (it has no export action).
- **Error reports (#65).** When Sentry lands, save the related events before its retention removes them.
- **Platform logs.** Save App Platform's deploy and runtime logs for the window, and the GitHub organization's audit log if the repository or CI may be involved.
- Keep the incident record private (a GitHub security advisory draft on the affected repository works well). Never copy personal data into it, an issue, a pull request, or a chat. Refer to players by OPEN Code.

### 3. Fix

- Fix the cause on a private branch or in the security advisory's private fork, with review by a second maintainer when one is available.
- Add a test that fails without the fix.
- Deploy, then confirm the containment steps can be undone safely (for example, register a new partner application with a new secret).

## Rating repair

Ratings that changed because of an incident are repaired only through the match ledger, which is append-only:

- Void or correct the bad matches with new records that reference the old ones, through the match workflow or the moderator bulk voids. Never edit or delete a counted match, a rating row, or a rating history row in place, not even from a database shell.
- Each void or correction requests a replay on its own (ADR 0028). The replay rebuilds every rating from the corrected ledger and records its provenance, so the repaired ratings can be reproduced. Check that the Celery `ratings` worker and beat are running, and that a new `ratings.RatingReplay` row appeared after the last void.
- Say which matches were voided, and why, in the incident record, by OPEN Code.

## Notifying players and partners

Notify every player whose personal data was exposed or whose rating changed because of the incident, and every partner whose application, tokens, or players were affected. Send player notices to the Account's login email through OPEN's email outbox. Keep each message plain: what happened, what it means for them, what we did, and what they should do.

### Template: players

> Subject: A security problem affected your OPEN account
>
> Hi,
>
> On [date], we found [a short, plain description of what happened]. It affected your OPEN account: [what was exposed or changed, for example "your email address and display name" or "two of your matches were changed without your confirmation"].
>
> What we have done: [for example: closed the problem on [date], ended all sign-in sessions, voided the changed matches and recalculated ratings].
>
> What you should do: [for example: sign in again, check your recent matches, and change your password if you use it on any other site]. [Or: you do not need to do anything.]
>
> We are sorry this happened. If you have questions, reply to this email or write to [security contact].
>
> The OPEN team

### Template: partners

> Subject: OPEN security incident affecting [application name]
>
> Hello,
>
> On [date and time, UTC], we found [a short description]. It affected [your application / players who use your application / data your application received].
>
> Scope: [what data or actions, the time window, and the number of affected players, without naming them].
>
> What we have done: [for example: revoked all access and refresh tokens for your application on [date and time, UTC]].
>
> What you need to do: [for example: rotate your client secret in [where], request new tokens, and tell us if you see unexpected activity].
>
> We will send an update by [date] and a summary when the investigation closes. Your contact for this incident is [name and address].
>
> The OPEN team

## Target timelines

Times run from when a report arrives or a maintainer first notices the problem.

| Step | Target |
| --- | --- |
| Acknowledge a report | Within 5 business days, as the security policy promises. Sooner for anything that looks like a live incident |
| Start containment | Within 24 hours of confirming an incident |
| Notify affected players and partners | Within 72 hours of confirming an incident, even when the investigation is still open, then update as facts change |
| Publish a summary | Within 30 days after the fix is deployed |

The summary is public, in plain language, and names no one: what happened, who was affected (as counts), what we changed so it does not happen again, and credit for the reporter unless they prefer to stay anonymous. Governance publishes security matters in summary only.

## Legal notification duties: open items

These are questions to check with counsel before launch. This plan does not answer them, and nothing here is legal advice.

- [ ] Which data breach notification laws apply to OPEN, given where players live (each US state's breach law, and any non-US players).
- [ ] Whether any law sets a deadline to notify a regulator or players that is shorter than the targets above, and who the regulator is.
- [ ] What counts as notifiable personal data under those laws, compared with the inventory in `docs/privacy/personal-data-inventory.md`.
- [ ] Extra duties for players aged 13 to 17, and for notifying a parent or guardian.
- [ ] Duties owed to partners under the partner terms, once those terms exist.
- [ ] Whether the notification templates above need wording that a law requires.
- [ ] Who in the project signs a regulator notification while there is no legal entity.

## After an incident

- Within two weeks of the fix, hold a short review: what happened, what went well, and what to change. Change this plan if a step was wrong or missing.
- File follow-up work as issues, without details that would help an attacker before the fix is out.

## Tabletop exercise

Before launch, a maintainer reads this plan through against a made-up incident (for example, "a partner's client secret was posted publicly") and records the result, and anything to change, on issue #105.
