# CLAUDE.md

This file gives Claude Code guidance for working in this repository.

## What this repository is

The backend for OPEN, the Open Pickleball Evaluation Network: a free, open-source, community-governed pickleball rating network. This repository will hold the services and APIs that store players and matches and serve ratings. The rating math itself lives in the separate `engine` repository.

Planned services, from the project roadmap:

- **Results API:** platforms, clubs, and tournaments submit matches.
- **Ratings and stats API:** any app can read OPEN ratings, history, and stats for free.
- **Organizer tools (backend side):** upload results, confirm starting ratings, review flagged matches.

## Current status

There is no code yet. No OPEN rating is live, and the language and framework are not final (Python is likely). Do not assume a stack. If a task needs one, raise it as a decision and record it in `docs/adr/` once made.

When code arrives, add the setup, test, lint, and run commands to the "Commands" section below and to `README.md`.

## Commands

None yet.

## Documentation

- `README.md`: public overview, setup, and contribution pointers.
- `CONTEXT.md`: the domain language (Match, Ledger, Replay, Rating Format, Reliability, and so on). Read it before naming models, endpoints, or fields.
- `docs/adr/`: architecture decision records. Check here before changing a decision, and add a record when you make one.
- Organization-wide policy lives in the `openpickleball/.github` repository: charter, governance, roadmap, contributing guide, security policy, licensing. A local checkout may exist at `../OPEN-pickleball`.
- The reference engine may be checked out at `../OPEN-engine`. Its module docstring in `src/open_engine/engine.py` lists production requirements the backend must meet.

## Hard rules

1. **No real player data, anywhere.** Not in code, fixtures, seed scripts, migrations, logs, issues, pull requests, or screenshots. Use made-up or properly de-identified data. `.gitignore` excludes local data directories and database files; keep it that way.
2. **Do not reimplement rating math.** The backend calls the engine. If a calculation seems wrong or missing, the fix belongs in the engine or the rating spec, not here.
3. **The match ledger is append-only.** Corrections are new records that reference the old one, never in-place edits or deletes of counted matches. Authoritative ratings come from a deterministic replay of the ledger; live updates between replays are provisional and must be labeled as such.
4. **Store replay provenance.** Persist each replay's `as_of` date, config version, and config fingerprint so any published rating can be reproduced.
5. **External ratings are a seed only.** An imported rating initializes a player and needs verified identity and evidence. It can never overwrite an OPEN-earned rating once OPEN matches exist. Show imported reliability separately from OPEN-earned reliability.
6. **Do not name competing rating systems** in code, docs, issues, or comments. Say "standard external rating system" or "external rating".
7. **Charter limits.** No paid tiers, premium features, ads, or payment flows. Players can always export their data. Do not build anything that conflicts with the [mission charter](https://github.com/openpickleball/.github/blob/main/CHARTER.md).
8. **Security-sensitive areas:** score confirmation, organizer verification, duplicate detection, identity linking, and anything that lets one party change another player's rating. Treat bypasses here as security bugs, not feature gaps.

## Contribution conventions

- Every commit needs a DCO sign-off (`git commit -s`). CI (`.github/workflows/dco.yml`) rejects pull requests with unsigned commits.
- One change per pull request, linked to an issue. Add or update tests and docs when behavior changes.
- License: AGPL-3.0-or-later for this repository. API specifications are Apache-2.0 and published separately, so do not place OpenAPI specs here as the canonical copy.
- Before proposing a feature, ask whether it makes the standard more accurate, more trusted, or easier to adopt. If not, it probably belongs in a partner's product.

## Writing style

Docs in this project use plain, short sentences. No emojis and no em dashes. Prefer "player", "match", and "organizer" over generic words like "user" or "entity" (see `CONTEXT.md`).
