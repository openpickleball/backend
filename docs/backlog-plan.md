# Backlog plan

Proposed reorganization of the backend backlog, written 2026-11-12. Nothing here is applied until a maintainer approves it; applied changes are logged in `docs/backlog-changes.md`.

## Context

The backlog (99 open issues in `openpickleball/backend`) was planned as five stage milestones (M0 Foundation to M4 Partner integration) plus two epics with no milestone (#95 rating widget, #98 security hardening). The stage milestones do not describe a shippable outcome, and the MVP (a league like Gateway Smash submits results, players see accurate ratings, in production with real players) is spread across all five. CSV upload, the only submission path the MVP needs, sits in M4. This plan regroups the work into outcome milestones, fixes dependency problems that would stall the MVP, and sets up labels, epics, native blocked-by links, and a project board for volunteers.

Decisions taken in planning (from the user):
- MVP submission path: **CSV upload** (#70, #71). The partner API moves to v0.2.
- Email provider: **Resend**, as the issues already say (better free tier). No provider rewrites needed.
- MVP target: **production with real players**. #93, #68, and the production-relevant parts of #98 are in v0.1.
- Self-reported external seeds (#49 to #51): **after MVP** (v0.4), off the legal-review blocker.

## Findings

**Counts.** 99 open issues (3 closed: #2, #3, #4). 84 in M0 to M4, 15 with no milestone (#1, #95 to #105). 31 labels. 2 epics: #95 uses real sub-issues (11 children); #98 lists 13 issues in its body but has no sub-issues. No issue uses native blocked-by yet, but the field works in this repo, so dependencies can be native. The org has issue types Task, Bug, Feature. 18 issues carry `good first issue`.

**Quality is high overall.** Almost every issue has context, tasks, acceptance criteria, "Depends on", and a size. Gaps are scope and consistency, not missing structure.

**Stale or outdated**
- #59, #61, #63 say "update the existing draft" Dockerfile, `.do/app.yaml`, `deploy.yml`. Those drafts are no longer in the working tree (Q12 resolved). Reword to "create".
- ADR numbering clash: #92 plans "ADR 0004" and #93 "ADR 0005", but `docs/adr/0004-settings-from-the-environment.md` exists and 0003 is taken. Renumber: #92 to ADR 0005, #93 to ADR 0006, #96's embed ADR to the next free number.
- #14: the README already has the Technology and setup sections. Rescope to what is still missing (compose, tests via pytest, lint, API docs).
- #16: first task already done.
- #1: Q12 is resolved, Q13 (Resend free tier limits) still needs its re-check, Q15 and F8 (no type labels) are superseded by the new label taxonomy, F7 and Q8 refer to M-milestones. Update after the apply.
- #98 says all its issues land "before production (#68)", but #28, #29, #84 are partner features that now ship after v0.1 production. Reword: each hardening item lands before the feature it protects ships.
- `CLAUDE.md` describes milestones "M0 Foundation through M4 Partner integration". Update with the new milestone names (repo file, in the docs PR).

**Duplicates and overlaps** (no exact duplicates, so no closures)
- #20 and #79 both add `ClubRole` and `ClubApplicationGrant`. Keep in #20, remove from #79.
- #28 and #83 both define the "read-only" scope set. Keep in #28, #83 references it.
- #29 and #40 both have the application approval admin action. Keep in #29, remove from #40 (this also drops #40's dependency on #29).
- #53 has "partner variants requiring `ratings:read`", which is the same work as #83. Move to #83.
- #73 has a "Log in with OPEN" section; #78 is that section. Remove the task from #73.
- #36 includes the rating notice template, which belongs with #58 (v0.4). Move it.
- #13 and #104 both add `uv sync --locked` and least-privilege permissions; #63 and #104 both pin actions to SHAs. Keep #104 as the verifier, note the overlap.
- #65 and #66 overlap on job liveness; already cross-referenced. No change.

**Dependency problems** (would stall the MVP)
1. #40 (moderator admin) depends on #29 (OAuth registration), which depends on #99 (MFA, L). This pulls partner OAuth into the CSV path. Fix: drop the approval task from #40 (see above).
2. #34 (throttling) depends on #32 (partner endpoints), and #53 (public ratings) depends on #34. Fix: split #34. Public and first-party throttling stays in v0.1 and depends on #5, #8. The per-application partner throttle becomes a new v0.2 issue.
3. #47 (recalculate after edits) depends on #33 (partner PATCH/DELETE) for the `match_changed` signal. In v0.1, voids come from moderators (#40) and disputes (#26). Fix: define `match_changed` and a shared void/edit service in #26; #33 and #40 emit it; #47 depends on #26, not #33.
4. #45 (rating state) depends on #39 (data export), which pulls auth and email onto the ratings chain. Fix: reverse it. #39 depends on #45 and includes rating history.
5. #75 (Log in with OPEN) depends on #73 (a guide), and #73 depends on #81 (legal review, blocked). Fix: drop #75's dependency on #73.
6. Implied cycle: #29 promises sandbox credentials from #74, while #74 depends on #72, which chains back to #29. Fix: #74 depends on #29 and #62 only; #29 creates `is_sandbox` applications, #74 enforces isolation.
7. #19 (Player model) lists only #17, #18, but #92 and #93 both say they block it. Add both.
8. #18 depends on #16 only because of ADR numbering. Drop.
9. External blockers on the critical path: #43 needs the engine repo to tag a release (Q4); #68 needs pilot approval. #81 needs a legal reviewer (now off the MVP path).

**Issues to split** (L, or M with L-sized scope)
- #93 (L): keep #93 as ADR 0006 plus the personal data inventory and CONTEXT terms (S, blocks #19). New: age screen and 13-and-older signup rule (M); encrypted personal fields and audited staff access (M); account deletion and retention (M). The "13 to 17 excluded from partner sharing" tasks move into #32, #83, #85, #75.
- #99 (L): keep #99 as TOTP, recovery codes, and the staff MFA requirement (M). New: passkeys (WebAuthn) for browser and Expo (M). The application-owner requirement already lives in #29.
- #29 (L-sized): keep registration, approval, revoke (M). New: self-serve client secret rotation (S).
- #84 (L-sized): new SSRF-safe outbound HTTP utility (M) used by #84 and #86; #84 keeps endpoint registration (M).
- #96 (L-sized): keep the iframe card route, CORS exception, headers, and ADR (M). New: the `card.js` web component (M).
- #49 (L-sized, v0.4): keep the reference model, attestation, and endpoint (M). New: seeding through the engine with replay persistence (M); duplicate external ID flagging (S).
- #34: split as described in dependency problem 2.
- #23 (L): **recommend keeping whole.** It is one model set and one migration; splitting it creates two migrations on the same tables. Label `size:L`.

**Gaps (new issues, v0.1)**
- **Player profile endpoints:** create and edit my Player profile and privacy settings, and a public profile read by OPEN ID. Nothing in the backlog exposes #19 through the API, yet CSV rows need OPEN player IDs (F13), so players must be able to get one. (M, p0)
- **Organizer event endpoints:** list and create Events for a verified organizer. #71 makes the uploader pick an Event, but nothing lets an organizer create one. (S, p0)
- #53 must label every rating as a demonstration rating while the engine is a demonstration package (Q4). Add the task.

**Good first issue changes**
- Remove from #24 (score validation is security-sensitive, hard rule 8), #50 (changes ratings), #70 (needs F12, F13, and hard rule 6 context).
- Add to #54 (small, well-specified cache).
- #7 and #12 are good first issues on the critical path. Keep them, but the board should show an assignee and a target date so they do not stall.
- `help wanted` on #81 (needs a legal reviewer).

---

## Milestones

Rename rather than replace, so links and closed history carry over.

| Milestone | From | Goal | Done when |
|---|---|---|---|
| **v0.1 MVP: league results and public ratings** | rename M0 (#1) | A verified organizer uploads league results as CSV in production, and players see their (demonstration-labeled) ratings. | A verified organizer uploads a results CSV in production; ratings update; any player's public rating loads from `/api/v1/players/{open_id}/ratings`; players can sign up, edit privacy, dispute, and export; the v0.1 security items are closed; a restore drill is recorded. |
| **v0.2 Partner API** | rename M4 (#5) | Approved platforms submit and read matches over OAuth2. | A sandbox platform registers, gets a client-credentials token, submits single and bulk matches, reads ratings in batch, following only the partner guide; legal review recorded. |
| **v0.3 Rating widget, account linking, and webhooks** | rename M1 (#2) | Platforms show live OPEN ratings, link players, and get `rating.updated` without polling. | #95's acceptance criteria. |
| **v0.4 Player stats, notices, and rating seeds** | rename M2 (#3) | Players get stats and rating notices, and can bring a self-reported external seed. | All v0.4 issues closed; seeds only after #81 recorded its outcome. |
| (delete) | M3 (#4) | Empty after the moves. | |
| No milestone | | Decision log #1, Django LTS upgrade #69 (blocked until April 2027). | |

## Epics (one milestone each)

| Epic | Milestone | Sub-issues |
|---|---|---|
| **New: Foundation** | v0.1 | #5, #6, #7, #8, #9, #10, #11, #12, #13, #14, #15, #16, #42, #90 |
| **New: Players, clubs, and the match ledger** | v0.1 | #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #41, #79, #92, #93 |
| **New: Accounts, email, and the player API** | v0.1 | #30, #31, #35, #36, #38, #39, Player profile endpoints (new), Age screen (new), Account deletion (new) |
| **New: Ratings pipeline** | v0.1 | #34, #43, #44, #45, #46, #47, #48, #52, #53 |
| **New: League results upload** | v0.1 | #40, #70, #71, Organizer event endpoints (new) |
| **New: Hosting on App Platform** | v0.1 | #59, #60, #61, #62, #63, #64, #65, #66, #68 |
| **#98 Security hardening** (reuse) | v0.1 | #67, #99, #100, #101, #102, #103, #104, #105, Passkeys (new), Encrypted personal fields (new). #17 and #93 move to the ledger epic (they block #19); #28, #29, #84 move to their own milestones' epics (sub-issues allow one parent). |
| **New: Partner match API** | v0.2 | #27, #28, #29, #32, #33, #72, #73, #74, #80, #81, #82, #83, Partner throttle (new), Secret rotation (new) |
| **#95 Partner rating widget** (reuse, retitle "Rating widget, account linking, and webhooks") | v0.3 | #54, #75, #76, #77, #78, #84, #85, #86, #87, #88, #96, #97, Card web component (new), SSRF-safe HTTP (new). Remove #29, #34, #53 (now v0.2 and v0.1). |
| **New: Player stats, notices, and seeds** | v0.4 | #37, #49, #50, #51, #55, #56, #57, #58, Seeding via engine (new), Duplicate external ID flags (new) |

## Ordered issue lists

Priority: **p0** on the MVP critical path or an external blocker of it; **p1** required for its milestone but parallel; **p2** later milestones. "Blocked by" is the corrected list. Critical path issues are **bold**.

### v0.1 MVP (topological order)

**Critical path:** **#7 → #12 → #17 → (#18 → #93) + #92 → #19 → #20 → #21 → #22 → #23 → #24 → #25 → #79 → #26 → #46 → #47 → #53 → #68**, joined by **#43 (engine tag) → #44 → #45 → #46** and **#70 → #71** for upload. The hosting chain (#13 → #59 → #60 → #61 → #62 → #63 → #66, #67 → #68) is shorter and runs in parallel.

| # | Issue | Epic | Type | Area | Pri | Size | Blocked by |
|---|---|---|---|---|---|---|---|
| **7** | Local dev with docker compose | Foundation | chore | infra | p0 | M | |
| 11 | Ruff, mypy, pre-commit | Foundation | chore | infra | p1 | S | |
| 16 | ADR 0001: why Django | Foundation | docs | | p1 | S | |
| **92** | Public identifiers (ADR 0005) | Ledger | feature | players, matches, security | p0 | M | |
| **12** | pytest, factory_boy | Foundation | chore | infra | p0 | S | 7 |
| 8 | Cache backend (Valkey) | Foundation | chore | infra | p1 | S | 7 |
| 90 | CODEOWNERS, editorconfig | Foundation | chore | infra | p1 | S | 11 |
| **18** | ADR 0002: unclaimed players | Ledger | spike | players | p0 | S | |
| **93** | ADR 0006: personal data (rescoped) | Ledger | spike | players, security | p0 | S | 18 |
| 5 | Mount django-ninja API | Foundation | feature | api | p1 | S | 12 |
| 9 | Celery queues | Foundation | chore | infra | p1 | M | 7, 12 |
| 10 | Static files and CORS | Foundation | chore | infra, security | p1 | S | 12 |
| 13 | GitHub Actions CI | Foundation | chore | infra | p1 | M | 11, 12 |
| **17** | Audit log | Ledger | feature | security | p0 | M | 12 |
| 6 | Health endpoints | Foundation | feature | api, infra | p1 | S | 5, 8 |
| 14 | README (rescoped) | Foundation | docs | | p1 | S | 13, 16 |
| 15 | CONTRIBUTING | Foundation | docs | | p1 | S | 14 |
| **19** | Player model | Ledger | feature | players | p0 | M | 17, 18, 92, 93 |
| **43** | Pin the engine (needs engine tag) | Ratings | chore | ratings | p0 | S | 13, status:blocked |
| 104 | Supply chain checks | #98 | chore | infra, security | p1 | S | 13 |
| 102 | Production security settings | #98 | feature | security | p1 | M | 10, 13 |
| 105 | security.txt, incident plan | #98 | feature | security | p1 | S | 5 |
| 65 | Opt-in Sentry | Hosting | feature | infra | p1 | M | 9 |
| 59 | Production Dockerfile | Hosting | chore | infra | p1 | M | 13 |
| **20** | Club and organizer models | Ledger | feature | players | p0 | M | 19 |
| **30** | allauth headless | Accounts | feature | auth | p0 | M | 5, 19 |
| 60 | PgBouncer pool | Hosting | chore | infra | p1 | S | 59 |
| **21** | Event model | Ledger | feature | matches | p0 | S | 20 |
| **22** | Custom OAuth Application model | Ledger | feature | partners | p0 | M | 17, 20 |
| 35 | Email via anymail and Resend | Accounts | feature | email | p1 | M | 9, 30 |
| 99 | MFA: TOTP, recovery codes, staff rule | #98 | feature | auth, security | p1 | M | 30 |
| **new** | Player profile endpoints | Accounts | feature | players, api | p0 | M | 19, 30, 92 |
| 61 | App spec | Hosting | chore | infra | p1 | M | 60 |
| **23** | Match and Game models | Ledger | feature | matches | p0 | L | 21, 22 |
| 36 | Email templates | Accounts | feature | email | p1 | S | 35 |
| 38 | Sending subdomain DNS doc | Accounts | docs | email | p1 | S | 35 |
| new | Passkeys (browser and Expo) | #98 | feature | auth, security | p1 | M | 99 |
| 100 | Account security limits | #98 | feature | auth, security | p1 | M | 30, 35, 99 |
| 101 | Harden the Django admin | #98 | feature | security | p1 | M | 17, 99 |
| new | Age screen and 13+ rule | Accounts | feature | auth, players | p1 | M | 19, 30, 93 |
| new | Encrypted personal fields, audited access | #98 | feature | security, players | p1 | M | 17, 19, 93 |
| new | Account deletion and retention | Accounts | feature | players, security | p1 | M | 19, 30, 93 |
| 62 | Staging environment | Hosting | chore | infra | p1 | M | 61 |
| **24** | Score validation | Ledger | feature | matches | p0 | M | 23 |
| **44** | Engine adapter | Ratings | feature | ratings | p0 | M | 23, 43 |
| **79** | Submission roles | Ledger | feature | matches, security | p0 | M | 20, 22, 23 |
| 63 | Deploy main to staging | Hosting | chore | infra | p1 | M | 62 |
| 67 | Backups and restore drill | #98 | chore | infra, security | p1 | M | 62 |
| **25** | Duplicate detection | Ledger | feature | matches, security | p0 | S | 24 |
| **70** | CSV results format spec | Upload | docs | matches | p0 | S | 24, 92 |
| **45** | Rating state and history models | Ratings | feature | ratings | p0 | M | 44 |
| **new** | Organizer event endpoints | Upload | feature | matches, api | p0 | S | 21, 30, 79 |
| 64 | Environment variable table | Hosting | docs | infra | p1 | S | 35, 63 |
| 66 | Logging, health checks, alerts | Hosting | chore | infra | p1 | M | 63 |
| **26** | Confirmation, `match_changed` signal | Ledger | feature | matches, security | p0 | M | 25, 79 |
| **40** | Moderator admin (no app approval) | Upload | feature | security | p0 | M | 26 |
| 103 | Restrict managed Postgres and Valkey | #98 | chore | infra, security | p1 | M | 62, 64 |
| **46** | Incremental rating update | Ratings | feature | ratings | p0 | M | 26, 45 |
| 31 | First-party submit, confirm, history | Accounts | feature | matches, api | p1 | M | 26, 30 |
| 41 | Seed command | Ledger | chore | infra | p1 | S | 26 |
| **71** | CSV upload with report | Upload | feature | matches, api | p0 | M | 40, 70, 79, new event endpoints |
| **34** | Throttling, public and first-party (rescoped) | Ratings | feature | api, security | p0 | S | 5, 8 |
| **47** | Recalculate after edits and voids | Ratings | feature | ratings | p0 | M | 26, 46 |
| 52 | Reliability decay schedule | Ratings | feature | ratings | p1 | M | 46 |
| **53** | Rating lookup and history (+ demo label) | Ratings | feature | ratings, api | p0 | M | 34, 45 |
| 39 | Player data export (+ rating history) | Accounts | feature | players, security | p1 | M | 31, 35, 45 |
| 42 | OpenAPI schema in CI | Foundation | chore | api | p1 | S | 13, 31 |
| 48 | Batch recompute command | Ratings | feature | ratings | p1 | M | 47 |
| **68** | Production environment | Hosting | chore | infra | p0 | M | 63, 66, 67, 103, status:blocked (pilot approval) |

### v0.2 Partner API

| # | Issue | Type | Area | Size | Blocked by |
|---|---|---|---|---|---|
| 27 | Ninja OAuth2 auth class | feature | partners, security | M | 22 |
| 80 | Match payload schema | feature | partners, api | M | 5, 23, 24 |
| 82 | Platform player ID mapping | feature | partners | M | 19, 20, 22 |
| 28 | Client credentials, scopes, lifetimes | feature | partners, security | M | 27 |
| 29 | App registration and approval (rescoped) | feature | partners, security | M | 28, 99 |
| new | Self-serve client secret rotation | feature | partners, security | S | 29 |
| new | Partner throttle per application | feature | partners, security | S | 27, 34 |
| 81 | Legal review (help wanted) | spike | partners | S | 80, status:blocked |
| 74 | Partner sandbox | feature | partners, infra | M | 29, 62 |
| 32 | Partner create and read (+ minors rule) | feature | partners, api | M | 26, 29, 79, 80, 82 |
| 33 | Partner update and delete | feature | partners, api | M | 32, 79 |
| 83 | Read-only access, batch lookup (+ partner variants from #53, + minors rule) | feature | partners, ratings | M | 28, 29, 53, 82 |
| 72 | Bulk match endpoint | feature | partners, api | M | 33, 79, 80, partner throttle |
| 73 | Partner OAuth2 guide | docs | partners | M | 72, 81 |

### v0.3 Rating widget, account linking, and webhooks (#95)

| # | Issue | Type | Area | Size | Blocked by |
|---|---|---|---|---|---|
| 54 | Cache public rating lookups (good first issue) | feature | ratings | S | 53 |
| new | SSRF-safe outbound HTTP | feature | security, partners | M | 9 |
| 75 | Log in with OPEN (+ minors rule) | feature | auth, partners | M | 29, 53, 82 |
| 96 | Embed: iframe card, CORS exception, ADR | feature | partners, security | M | 53, 54, 102 |
| new | Embed: `card.js` web component | feature | partners | M | 96 |
| 76 | Consent screen | feature | auth, partners | M | 30, 75 |
| 77 | List and revoke linked apps | feature | auth, partners | S | 76 |
| 78 | Account linking guide | docs | partners | S | 73, 77 |
| 84 | Webhook endpoint registration | feature | partners, security | M | 29, 83, SSRF utility |
| 85 | Webhook outbox and emission (+ minors rule) | feature | partners | M | 26, 33, 46, 53, 84 |
| 86 | Signed delivery with retries | feature | partners, security | M | 9, 85, SSRF utility |
| 87 | Disable failing endpoints, delivery log | feature | partners, email | M | 35, 86 |
| 88 | Webhook test events | feature | partners | S | 74, 87 |
| 97 | Partner quick start | docs | partners | S | 53, 74, 75, 81, 85, 86, 96 |

### v0.4 Player stats, notices, and rating seeds

| # | Issue | Type | Area | Size | Blocked by |
|---|---|---|---|---|---|
| 55 | Stats: performance vs expected | feature | ratings, api | M | 53 |
| 56 | Stats: partners and head-to-head | feature | ratings, api | M | 53 |
| 57 | Stats: percentile | feature | ratings, api | S | 54 |
| 37 | Confirmation digest emails | feature | email | M | 26, 36 |
| 58 | Rating notice emails (+ template from #36) | feature | email, ratings | S | 37, 46 |
| 49 | Self-reported external seed: reference and attestation | feature | ratings, players | M | 19, 45, 81 |
| new | Seed through the engine, persisted for replay | feature | ratings | M | 48, 49 |
| new | Flag duplicate external player IDs | feature | ratings, security | S | 40, 49 |
| 50 | Moderator admin for starting ratings | feature | ratings, security | S | 40, 49 |
| 51 | Sandbagging checks | feature | ratings, security | M | 40, seed issue |

Size and priority for v0.2 to v0.4: all `priority:p2`.

---

## Labels

New set: `type:feature`, `type:chore`, `type:docs`, `type:spike`, `type:epic`; `area:api`, `area:auth`, `area:email`, `area:infra`, `area:matches`, `area:partners`, `area:players`, `area:ratings`, `area:security`; `priority:p0`, `priority:p1`, `priority:p2`; `size:S`, `size:M`, `size:L`; `status:blocked`. No `area:frontend`: no backend issue is frontend work (that lives in the frontend repo).

| Old | New | How |
|---|---|---|
| api | area:api | rename |
| auth | area:auth | rename |
| email | area:email | rename |
| ratings | area:ratings | rename |
| security | area:security | rename |
| partners | area:partners | rename |
| devops | area:infra | rename |
| ci | area:infra | add area:infra, then delete |
| testing | area:infra | add area:infra, then delete |
| oauth | area:partners (area:auth for #76, #77) | relabel, then delete |
| webhooks | area:partners | relabel, then delete |
| data | area:players or area:matches per issue | relabel, then delete |
| backend | (none) | delete; every issue here is backend |
| documentation | type:docs | rename (supersedes Q15/F8 in #1) |
| blocked | status:blocked | rename |
| enhancement, bug, triage | keep | org issue templates apply them; triage swaps `enhancement` for a `type:` label |
| good first issue, help wanted, question, duplicate, invalid, wontfix, accessibility, governance, rating-spec, feedback, needs-evidence, ready-for-human-review | keep | org-wide or used by the `/work-issue` workflow |

To delete (after relabeling): `backend`, `data`, `oauth`, `ci`, `testing`, `webhooks`.

## Project board

Org project "OPEN backend" linked to this repo. Fields: Status (Backlog, Ready, In Progress, In Review, Done), Priority (p0, p1, p2), Size (S, M, L), and the built-in Milestone field. Add all open issues; v0.1 issues with no open blockers start in Ready, the rest in Backlog. Views: Board by Status; "Ready for volunteers" (Status = Ready, no assignee, sorted by Priority); "Good first issues" (label `good first issue`, open); Roadmap by Milestone. Fields and items can be set through the API; views likely need the web UI, so I will give exact click steps if the API refuses.

## Supporting repo files (recommendations, not part of the apply)

- **Issue templates:** the org provides bug and feature templates without acceptance criteria and no epic template. A repo-level `.github/ISSUE_TEMPLATE/` replaces the org set for this repo, so it must also carry the bug template and a `config.yml` linking to the org community feedback and rating spec forms. Recommend repo templates: Task (Context, Tasks, Acceptance criteria, Depends on, Size: the shape existing issues already use), Bug (org fields plus expected behavior and acceptance criteria), Epic (Problem, tracked sub-issues, Done criteria, Out of scope). File as a new v0.1 Foundation issue.
- **PR template:** the org template already has `Closes #` and "links the issue". Keep it (#90 already decided not to add a repo one).
- **CONTRIBUTING.md:** add to #15's scope a "Picking up work" section: use the "Ready for volunteers" view, comment to claim, one issue per PR, `Closes #N`, what p0 and good first issue mean, and that blocked-by links must be closed first.

## Exact changes, grouped

1. **Labels (batch 1):** create 18 labels; rename 9 (`api`, `auth`, `email`, `ratings`, `security`, `partners`, `devops`, `documentation`, `blocked`); delete 6 after batch 4 relabels.
2. **Milestones (batch 2):** rename M0, M4, M1, M2 with new titles and descriptions (goal plus done criteria); delete M3 after its issues move.
3. **Epics and sub-issues (batch 3):** create 7 new epic issues (`type:epic`); retitle #95; add #98's 10 children as real sub-issues; remove #29, #34, #53 from #95; add sub-issue links for every issue listed in the Epics table.
4. **Issue edits (batch 4):** for all 99 issues: set milestone, `type:`, `area:`, `priority:`, `size:` labels; remove retired labels; add native blocked-by links from the tables (about 200); body edits for: drafts wording (#59, #61, #63), ADR numbers (#92, #93, #96), overlaps (#20/#79, #28/#83, #29/#40, #53/#83, #73/#78, #36/#58), dependency fixes (#19, #18, #26, #34, #39, #40, #45, #47, #74, #75), demo label (#53), minors rule moved into #32, #75, #83, #85, rescopes (#14, #16 tick, #98 wording, #1 status of Q12/Q15/F8). Update every "Depends on" line to match the native links. Good first issue and help wanted changes.
5. **Splits (batch 5):** create 13 new issues (3 from #93, 1 from #99, 1 from #29, 1 from #34, 1 from #84, 1 from #96, 2 from #49, 2 gap issues, 1 issue-templates issue), each with context, tasks, acceptance criteria, blocked-by, labels, milestone, epic; trim the moved tasks from the source issues with a link.
6. **Closures (batch 6):** none planned.
7. **Repo files (docs PR):** `docs/backlog-plan.md`, `docs/backlog-changes.md`, and the `CLAUDE.md` milestone sentence.

## Decisions on the open questions

Approved by the maintainer with the recommended defaults:

1. **Players without accounts.** For the pilot, every player signs up and gets an OPEN ID before their matches are uploaded (F13 stands). Organizer-created unclaimed players wait for ADR 0002.
2. **Production gate.** The governing board approves the pilot that unblocks #68. #98 is reworded so each hardening item lands before the feature it protects ships.
3. **Type.** Use `type:` labels as planned. The org issue types are not used for now.
4. **Milestones.** Rename M0, M4, M1, M2 as planned, and delete M3 when it is empty.
5. **#23** stays whole at `size:L`.
6. **Passkeys and decay** stay in v0.1 at p1.
7. **Privacy policy and terms** belong in the org `.github` repository. Publishing them gates #68; add that task to #68.
8. **Engine tag.** Still open: a maintainer asks the engine repository for the `v0.1.0` tag (#43, Q4).

## New issue numbers

Epics: #106 Foundation, #107 Players, clubs, and the match ledger, #108 Accounts, email, and the player API, #109 Ratings pipeline, #110 League results upload, #111 Hosting on App Platform, #112 Partner match API, #113 Player stats, notices, and seeds.

Issues called "new" in the tables above: #114 age screen and 13+ rule, #115 encrypted personal fields, #116 account deletion and retention, #117 passkeys, #118 client secret rotation, #119 partner throttle, #120 SSRF-safe outbound HTTP, #121 `card.js` web component, #122 seeding through the engine, #123 duplicate external ID flags, #124 player profile endpoints, #125 organizer event endpoints, #126 issue templates.
