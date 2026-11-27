# The rating engine

All rating math comes from the engine, a separate repository
([openpickleball/engine](https://github.com/openpickleball/engine)). The
backend installs it as a package and calls it. It never copies or reimplements
the math (hard rule 2 in `CLAUDE.md`).

## The pin

The engine is a git dependency pinned to a release tag (decision Q4 in
issue #1). `pyproject.toml` names the tag:

```toml
"openpickleball-engine @ git+https://github.com/openpickleball/engine@v1.0.0",
```

`uv.lock` records the same tag and the full commit it pointed to when it was
locked:

```toml
source = { git = "https://github.com/openpickleball/engine?rev=v1.0.0#be2a0ff59065f7273b5dc13e395e5ec25ee01d3c" }
```

`uv sync` installs that commit, even if someone later moves the tag. The
distribution is `openpickleball-engine`. The import name is `open_engine`.

Installing the engine from its git tag needs git wherever `uv sync` runs: the
host, the compose containers, and any deploy build.

`apps/ratings/engine_adapter.py` is the only application module that imports
`open_engine` (tests aside, and a test checks it). Other code goes through that
module.

## The adapter

The adapter converts backend records to engine inputs and engine results to
plain data. It does no rating math.

- `to_engine_match(match)` converts one saved Match. `counted_engine_matches()`
  converts every Match that counts (`Match.objects.counted()`) in a fixed
  number of queries.
- The `match_id` is the Match's OPEN Code, and each player id is the Player's
  OPEN Code.
- The match type is stored on each Match when it is recorded (ADR 0035).
  `confirmed_by` and the stored rating format follow ADR 0024.
- Every stored value goes through an explicit table. A value the table does not
  know raises `EngineMappingError`, which names the field.
- `load_config()` loads the published parameter set that
  `OPEN_ENGINE_PARAMETER_SET` in `config/settings.py` names (`v1`). It is a
  fixed value in code, not an environment variable, because a new set needs a
  full replay.
- `rating_update()`, `replay_provenance()`, and `rating_summary()` turn an
  engine `MatchResult`, `ReplayInfo`, and `PlayerSummary` into plain
  dataclasses. `replay_provenance()` adds the installed engine version (hard
  rule 4). `rating_summary()` keeps imported reliability apart from
  OPEN-earned reliability (hard rule 5), and `apps.ratings.services` stores
  its values (ADR 0025).

## The rating spec version

`OPEN_RATING_SPEC_VERSION` in `config/settings.py` names the
openpickleball/rating-spec tag the ratings follow: tag `v1.0.0rc1`, stored as
`1.0.0rc1`. Every rating
state, history row, and replay stores a spec version, by default this one (ADR
0025). Bump it when rating-spec
tags v1.0.0, deploy, then run the recompute (`recompute_ratings`, below).

## The engine version

`apps.ratings.engine_adapter.engine_version()` returns the installed engine's
version, such as `1.0.0`:

```bash
uv run python manage.py shell -c "from apps.ratings.engine_adapter import engine_version; print(engine_version())"
```

The tests in `apps/ratings/tests/test_engine_adapter.py` check that the tag in
`pyproject.toml`, the tag in `uv.lock`, and the installed version agree. A
stale lock is caught earlier: CI's `uv sync --locked` fails when `uv.lock` does
not match `pyproject.toml`, and so does `uv lock --check`.

The dependency audit (`scripts/audit_dependencies.py`, see CONTRIBUTING.md)
leaves the engine out of pip-audit, since no vulnerability database tracks it,
and fails instead unless `uv.lock` pins it to a version tag and a full commit
SHA. Dependabot is set to leave it alone (`.github/dependabot.yml`): follow the
steps below.

## Upgrading the engine

1. Read the engine's `CHANGELOG.md` for every version between the current tag
   and the new one. Note each breaking change, each changed default, and each
   new or changed parameter set. If the release publishes a new set,
   decide whether `OPEN_ENGINE_PARAMETER_SET` moves to it (a full replay follows).
2. Bump the tag with uv. Never edit `pyproject.toml` or `uv.lock` by hand:

   ```bash
   uv add --raw "openpickleball-engine @ git+https://github.com/openpickleball/engine@vX.Y.Z"
   ```

   `--raw` keeps the git URL in `pyproject.toml`, where the tests look for it.
   Check that `uv.lock` names the new tag and a new commit.
3. Run the full test suite (`uv run pytest -n auto`) and fix what the new
   version breaks in the adapter, never by copying engine code.
4. Run the backtest once under each version, each in its own environment.
   Score the same ledger matches with the engine's evaluation (`evaluate`),
   then compare the two reports. A change that makes predictions worse needs
   a reason before it ships. The backend has no backtest command yet.
5. Open one pull request with the bump, the changelog notes from step 1, and
   the backtest result.
6. Once it is merged and deployed, run the recompute (below), so every
   published rating comes from it (hard rule 3).

## The recompute

`manage.py recompute_ratings` rebuilds every rating with a full replay of
the counted ledger (ADR 0058). Each write stores its replay's `as_of` date,
config version, config fingerprint, and engine and spec versions (hard
rule 4). Run it after an engine upgrade, a change of
`OPEN_ENGINE_PARAMETER_SET`, or a bump of `OPEN_RATING_SPEC_VERSION`, once
the change is deployed. Run a dry run first:

```bash
uv run python manage.py recompute_ratings --dry-run --verbosity 2
uv run python manage.py recompute_ratings
```

It writes in one transaction, like the regular replay, so live updates wait
for it, and it is safe to run again. It clears the replay requests that were
pending when it took the engine snapshot lock. `--dry-run` writes nothing and prints how many
ratings would be created, changed, removed, or left alone (at verbosity 2,
per rating format). It names no player. Only a dry run takes `--format`, an
`--as-of` before the latest counted match or after today (in UTC, or the
latest counted match date when later), or a `--spec-version` other than the
setting, so you can compare a replay without changing stored ratings.
