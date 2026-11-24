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

Only `apps/ratings/engine_adapter.py` imports `open_engine`. Other code goes
through that module.

## The engine version

`apps.ratings.engine_adapter.engine_version()` returns the installed engine's
version, such as `1.0.0`:

```bash
uv run python manage.py shell -c "from apps.ratings.engine_adapter import engine_version; print(engine_version())"
```

The tests in `apps/ratings/tests/test_engine_adapter.py` check that the tag in
`pyproject.toml`, the tag in `uv.lock`, and the installed version agree. A tag
bump that forgets to relock fails them.

## Upgrading the engine

1. Read the engine's `CHANGELOG.md` for every version between the current tag
   and the new one. Note each breaking change, each changed default, and each
   new or changed parameter set.
2. Bump the tag with uv. Never edit `pyproject.toml` or `uv.lock` by hand:

   ```bash
   uv add --raw "openpickleball-engine @ git+https://github.com/openpickleball/engine@vX.Y.Z"
   ```

   `--raw` keeps the git URL in `pyproject.toml`, where the tests look for it.
   Check that `uv.lock` names the new tag and a new commit.
3. Run the full test suite (`uv run pytest -n auto`) and fix what the new
   version breaks in the adapter, never by copying engine code.
4. Run the backtest: replay the ledger's matches with the new version and
   compare its predictions with the old version's. The engine's evaluation
   tools do this. A change that makes predictions worse needs a reason before
   it ships. The backend has no backtest command yet.
5. Run the recompute: replay the whole ledger with the new version so every
   published rating comes from it (hard rule 3). Each replay stores its
   `as_of` date, config version, and config fingerprint (hard rule 4). The
   recompute command comes with issue #48.
6. Open one pull request with the bump, the changelog notes from step 1, and
   the backtest result.
