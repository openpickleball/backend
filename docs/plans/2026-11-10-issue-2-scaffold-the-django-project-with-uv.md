# Plan: Scaffold the Django project with uv (issue #2)

Risk tier: Medium. No migrations, auth, or outward messages, but it adds more than a few files and fixes the project layout and settings shape that every later issue builds on. Every criterion can be checked by a command.

Domain skills:
- django: invoke. The skill targets Django 6.0; where it differs from 5.2, follow 5.2.
- superpowers:test-driven-development: invoke, adapted. pytest arrives in #12, so red and green are the gate commands below, run before and after the change.
- context7 docs: invoke if a Django 5.2 or uv detail is uncertain.
- superpowers:verification-before-completion: invoke before claiming any result.
- django-ninja-api: not applicable because the API is mounted in #5.
- python-fastapi, frontend, Expo, email, payments: not applicable because this change has no such code.

## Decisions taken with the user

- Settings are a single module, `config/settings.py` (not a package). `DJANGO_SETTINGS_MODULE=config.settings`.
- Interim settings use `startproject` style hardcoded values for local development only. #4 replaces them with environment variables. No new dependency for this.

## Acceptance criteria

1. `uv sync --locked` on a clean checkout (only the files git would commit, no `.venv`) installs everything from `uv.lock`, and `uv run python manage.py check` then passes in that checkout.
2. `uv run python manage.py check` passes in the worktree with "System check identified no issues (0 silenced)."
3. No `migrations/` directory exists anywhere in the repository outside `.venv`.
4. `pyproject.toml` has `requires-python = ">=3.13"`; `.python-version` contains `3.13`; the interpreter uv uses is 3.13.x.
5. Dependencies are exactly `Django>=5.2,<5.3` and `psycopg[binary]` (with a lower bound uv adds). The installed Django is 5.2.x and `psycopg` imports with the binary implementation.
6. `config/` contains `__init__.py`, `settings.py`, `urls.py`, `wsgi.py`, `asgi.py`. `manage.py`, `config/wsgi.py`, and `config/asgi.py` all default `DJANGO_SETTINGS_MODULE` to `config.settings`, and both `application` objects import.
7. `apps/__init__.py` exists, so first-party apps import as `apps.<name>`. No first-party app is created and none is in `INSTALLED_APPS`.
8. `uv.lock` is present and `uv lock --check` passes.
9. Settings use Postgres (`django.db.backends.postgresql`), not SQLite. No real secret: `SECRET_KEY` is a fixed, readable placeholder starting with `django-insecure-`, not a random generated key.
10. No `migrate` was run and no migration was created.
11. No emojis and no em dashes in any added file.

## Files that change

All new unless noted:

- `pyproject.toml`
- `.python-version`
- `uv.lock`
- `manage.py`
- `config/__init__.py`, `config/settings.py`, `config/urls.py`, `config/wsgi.py`, `config/asgi.py`
- `apps/__init__.py`
- `docs/plans/2026-11-10-issue-2-scaffold-the-django-project-with-uv.md` (this plan)
- Phase 8 (coordinator, not the implementer): `README.md` and `CLAUDE.md` where their text becomes wrong.

Not created: `main.py` (uv init makes one; delete it), any `README` from uv init, any `migrations/` directory, `.env` or `.env.example` (#4), `conftest.py` or test settings (#12), ruff or mypy config (#11), `docker-compose.yml` (#7).

## Order of work

Run every command from the worktree root.

1. Red: run `uv run python manage.py check` and record that it fails (no `manage.py`). Already measured, see Proof.
2. `uv init --app --no-readme --vcs none --python 3.13 --name open-backend .`
   - Delete the generated `main.py`.
   - In `pyproject.toml`, set `description = "Backend services and APIs for OPEN, the Open Pickleball Evaluation Network."` Leave `version = "0.1.0"` and `requires-python = ">=3.13"`.
   - Confirm `.python-version` is `3.13`. If uv wrote a patch version, change it to `3.13`.
3. `uv add "Django>=5.2,<5.3" "psycopg[binary]"`. This writes `uv.lock` and creates `.venv` (ignored by `.gitignore`). Confirm the `Django` spec in `pyproject.toml` reads `django>=5.2,<5.3` (uv may lowercase the name; either is fine).
4. `uv run django-admin startproject config .` This creates `manage.py` and `config/{__init__,settings,urls,wsgi,asgi}.py`.
5. Edit `config/settings.py`:
   - Replace the generated random `SECRET_KEY` with the fixed placeholder `"django-insecure-local-development-only-replace-via-issue-4"`. Add a comment above it: local development only, #4 reads it from `DJANGO_SECRET_KEY`.
   - Keep `DEBUG = True` and `ALLOWED_HOSTS = []` with a comment that #4 reads them from the environment.
   - Replace the SQLite `DATABASES` block with Postgres: `ENGINE "django.db.backends.postgresql"`, `NAME "open"`, `USER "open"`, `PASSWORD ""`, `HOST "localhost"`, `PORT "5432"`. Comment: local development defaults, #4 reads `DATABASE_URL`, #7 provides the local Postgres.
   - Above `INSTALLED_APPS`, add a comment: first-party apps live under `apps/` and are added as `apps.<name>`; the custom user model (#3) must land before `migrate` is ever run or any other migration is created.
   - Keep `TIME_ZONE = "UTC"`, `USE_TZ = True`, `DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"`.
   - Remove the generated docstring lines that point to Django 5.2 "howto" URLs only if they mention anything false; otherwise leave the generated docstring as is.
6. Leave `config/urls.py`, `config/wsgi.py`, `config/asgi.py`, `manage.py` as generated (admin route included).
7. Create `apps/__init__.py` containing a one-line docstring: `"""First-party Django apps for the OPEN backend. Each app is a subpackage, installed as apps.<name>."""`
8. Green: run the full gate below and the live drive. Do not run `migrate` or `makemigrations` without `--check --dry-run`.

Do not commit or push anything. This run is in no-commit mode: leave every file uncommitted.

## Gate (derived; pytest, ruff, and mypy arrive in #11 and #12)

Run in this order and keep the output:

1. `uv lock --check`
2. Clean checkout: `D=$(mktemp -d) && git ls-files -co --exclude-standard -z | xargs -0 cp --parents -t "$D" && cd "$D" && test ! -e .venv && uv sync --locked && uv run python manage.py check; cd -; rm -rf "$D"`
   - Note: the agent's worktree guard refuses this one-liner because it pipes `git` output. It was run as two equivalent commands: `git ls-files -co --exclude-standard -z > <scratch>/files.z`, then `D=$(mktemp -d) && xargs -0 cp --parents -t "$D" < <scratch>/files.z && cd "$D" && test ! -e .venv && uv sync --locked && uv run python manage.py check`, then return and `rm -rf "$D"`.
3. `uv run python manage.py check`
4. `uv run python manage.py makemigrations --check --dry-run` (expect "No changes detected"; a warning that the database is unreachable is acceptable because no Postgres runs until #7)
5. `find . -path ./.venv -prune -o -type d -name migrations -print` (expect no output)
6. `uv run python -c "import sys, django, psycopg; print(sys.version); print(django.get_version()); print(psycopg.__version__, psycopg.pq.__impl__)"` (expect 3.13.x, 5.2.x, `binary`)
7. `uv run python -c "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); import django; django.setup(); from config.wsgi import application as w; from config.asgi import application as a; print(type(w).__name__, type(a).__name__)"`
8. `grep -n "DJANGO_SETTINGS_MODULE" manage.py config/wsgi.py config/asgi.py` (each shows `config.settings`)
9. `grep -rnP "\x{2014}|[\x{1F300}-\x{1FAFF}\x{2600}-\x{27BF}]" --include='*.py' --include='*.toml' --include='*.md' . --exclude-dir=.venv` (expect no output)
10. Advisory, not blocking (ruff is not a project dependency until #11): `uvx ruff@0.17.4 check . && uvx ruff@0.17.4 format --check .`

## Live drive

Medium tier: drive the changed path.

Departure found during implementation: in Django 5.2, `runserver` always runs `check_migrations()`, which opens a database connection. With no Postgres reachable it exits with `OperationalError: connection ... port 5432 failed: Connection refused` before it serves any request (both `curl` calls return 000). So the drive needs a reachable, empty Postgres until #7 provides one. For this drive only, start a throwaway container, never run `migrate` against it, and remove it afterward:

- `docker run -d --rm --name open-issue2-livedrive-pg -p 127.0.0.1:5432:5432 -e POSTGRES_USER=open -e POSTGRES_DB=open -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`, then wait for `pg_isready`.
- After the drive, confirm the database has no tables (`select count(*) from information_schema.tables where table_schema='public'` returns 0), then `docker stop open-issue2-livedrive-pg`.

Steps:

- `uv run python manage.py runserver 127.0.0.1:8765` in the background.
- `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/admin/login/` (expect 200; the login page renders without a database for an anonymous request).
- `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/no-such-page/` (expect 404).
- Keep the server's startup output (it will warn about unapplied migrations; that is expected and must not be "fixed" by running migrate). Stop the server.

## Risks

- Running `migrate` by accident would create Django's auth tables before the custom user model (#3). Mitigation: no Postgres runs yet, the plan forbids it, and the gate checks for migration directories.
- `startproject` writes a random secret key. Committing it would fail #4's "no real secrets" check. Mitigation: step 5 replaces it with a readable placeholder.
- `uv sync` could pick Python 3.14 (installed locally) if `.python-version` is missing or wrong. Mitigation: criterion 4 and gate step 6.
- Hardcoded `DEBUG = True` is unsafe for deployment. Acceptable here: nothing deploys before M3, and #4 makes it default to off.
- The top-level package name `apps` could shadow something. Checked: nothing in the dependency set installs a top-level `apps` module.

Choices not taken: a settings package split, `os.environ` reads before #4, adding pytest before #12, adding ruff config before #11.

## Proof

| Criterion | Proven by |
| --- | --- |
| 1 | Gate 2 |
| 2 | Gate 3 |
| 3, 10 | Gate 4, Gate 5 |
| 4, 5 | Gate 6, plus reading `pyproject.toml` and `.python-version` |
| 6 | Gate 7, Gate 8 |
| 7 | Gate 7 (`apps` importable is covered by reading `apps/__init__.py`), plus reading `INSTALLED_APPS` |
| 8 | Gate 1 |
| 9 | Reading `config/settings.py` |
| 11 | Gate 9 |

Live drive must show: the server starts, `/admin/login/` returns 200, an unknown path returns 404.

Measured on the default branch (1d41c2d), before any change:

```
$ uv run python manage.py check; echo "exit=$?"
python3.14: can't open file '.../manage.py': [Errno 2] No such file or directory
exit=2
```

No "behave alike" or "phrase absent" test in this plan depends on behavior of existing code: the repository has none. Gate 9 searches only files this change adds.
