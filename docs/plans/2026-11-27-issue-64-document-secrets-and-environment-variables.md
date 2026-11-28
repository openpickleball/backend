# Plan: Document secrets and environment variables (issue #64)

Risk tier: Low. A README table and a test that reads source files. No settings behavior changes, no migration, no API change.

Domain skills:
- django: not applicable because no Django code changes; the test reads `config/settings.py` as source text.
- django-ninja-api, django-allauth, django-celery: not applicable because no API, auth, or task code changes.
- superpowers:test-driven-development: invoke. The test is written first and fails until the table exists.
- superpowers:verification-before-completion: invoke before any success claim.

## Decisions

- The maintainer approved working this before its blocker #63 (the deploy workflow). The table says what #63 will add (a `production` GitHub environment holding the DigitalOcean token) without naming its secrets; a comment on #63 asks it to add its rows.
- Sentry variables do not exist yet. #65 adds them, and its own task list already says to add them to this table. No Sentry rows are invented here; a comment on #65 says the test will require its rows.
- OAuth token lifetimes and scopes are not environment variables today (`OAUTH2_PROVIDER` in settings, #28). The section says so instead of inventing names.
- "Settings" means the files that read the process environment at startup: `config/settings.py` and `gunicorn.conf.py`. No other first-party module reads `os.environ` today (checked with grep, tests aside).

## Acceptance criteria

1. README has an "Environment variables" section with one table row per variable that `config/settings.py` or `gunicorn.conf.py` reads: name, purpose, required or optional, secret or not, an example placeholder, and where production sets it.
2. The section covers Django, database, cache, Celery, Resend (`RESEND_API_KEY`), CORS, and says where OAuth lifetimes and scopes and Sentry stand.
3. The section explains where secrets are set (App Platform console, declared in `.do/production.yaml`; GitHub environments from #63; local `.env`) and that none are committed. It also lists the PWA's build-time variables in `.do/production.yaml` and says `.do/sites.yaml` sets none.
4. A test fails when settings or gunicorn reads a variable the table does not list, including a read through a helper or a loop over names, and fails when it cannot tell which variable a module-level read names.
5. The test fails when the table lists a backend variable nothing reads.
6. The test fails when an app-level env in `.do/production.yaml` is missing from the table, or when the table's secret and production columns disagree with the spec (`type: SECRET`).
7. No real values: every secret row's example is an angle-bracket placeholder, and the test checks it.

## Files that change

- `README.md`: new "Environment variables" subsection under "Setup and tests", after "Settings".
- `config/tests/test_env_var_table.py`: new test.
- `.claude/rules/settings.md`: one sentence telling future editors to add a row and a `.env.example` entry with each new variable.
- `docs/plans/2026-11-27-issue-64-document-secrets-and-environment-variables.md`: this plan.

## Order of work

1. Commit this plan.
2. Write the test (extractor over `ast`, table parser, spec checks), run it red.
3. Write the README section and table, run it green.
4. Full gate, review, PR.

## Risks

- The extractor misses a read style added later. Mitigation: any call at module level to a reader helper (a function that passes its parameter to `_raw` or `os.environ`, found transitively) whose name argument is not a string literal or a loop over literals fails the test, and so do other spellings of the environment (`from os import environ`, an alias of `os`, an alias of a reader helper, `Env.ENVIRON`, `os.environb`). A read through a function from another module would still pass unseen; settings use only their own helpers. A unit case on made-up source proves the helper path and the loop path.
- The table goes stale against `.do/production.yaml`. Mitigation: criterion 6.

## Proof

- Criteria 4 and 5: `test_every_read_variable_is_in_the_table`, `test_every_row_is_read`, and the made-up source cases.
- Criterion 6: `test_production_spec_agrees_with_the_table`.
- Criterion 7: `test_secret_examples_are_placeholders`.
- The README's claim that `.env.example` lists every variable: `test_env_example_names_every_variable`.
- Measured on origin/main: an AST pass over `config/settings.py` finds 56 literal names through the helpers plus 4 Celery names through the refusal loop; `gunicorn.conf.py` adds `PORT` and `WEB_CONCURRENCY`; all 62 appear in `.env.example`.
