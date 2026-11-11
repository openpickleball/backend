# Plan: Add a custom user model before the first migration (issue #3)

Risk tier: High. Forced: the change touches authentication and creates the project's first database migration, which every later migration depends on.

Domain skills:
- django: invoke. The skill targets Django 6.0; where it differs from 5.2, follow 5.2 (installed: 5.2.19).
- superpowers:test-driven-development: invoke. Tests use Django's runner (`manage.py test`), decided with the user, because pytest arrives in #12.
- superpowers:verification-before-completion: invoke before claiming any result.
- context7 docs: invoke if a Django 5.2 auth or admin detail is uncertain.
- django-ninja-api: not applicable because this change adds no API code (#5 mounts the API).
- python-fastapi, frontend, Expo, email, payments: not applicable because this change has no such code.
- django-allauth: not applicable because allauth arrives in #30.

## Decisions taken with the user

- `first_name` and `last_name` are removed from `User` (set to `None`). `User` holds login data only; names belong to Player (#19).
- Tests are `django.test.TestCase` classes run with `uv run python manage.py test`, against a throwaway Postgres container. pytest-django (#12) will run them unchanged.
- Round R3: the user chose ASCII-only email addresses, enforced at every layer (field validator, manager, database check constraint), to prevent lookalike (homoglyph) accounts such as `ınfo@example.org` (U+0131) next to `info@example.org`. The check applies after normalization (NFKC, strip, per-character lowercase), so `inſo@example.org` (U+017F) folds to `inso@example.org` and is accepted. The user approved rewriting six frozen tests (`LookupMatchesConstraintTests` in `test_review_fixes.py`, `FinalSigmaTests` in `test_review_fixes_2.py`), because each created a non-ASCII account with `create_user` and expected success, which the new policy forbids. The rewrites still guard the original defects.
- Round R4: the user asked for type hints on the accounts production code in this PR (`models.py`, `validators.py`, `forms.py`; `admin.py` and `apps.py` define no functions). Annotations are runtime-safe only: no new dependencies and no imports from django-stubs (mypy and django-stubs arrive in #11). `BaseUserManager["User"]` and `QuerySet["User"]` subscript at runtime on Django 5.2.19 (checked). Mutable Django class attributes use `ClassVar`. Behavior does not change. Pydantic is not applicable here; it arrives with django-ninja schemas in #5.
- Round R7: the user approved unfreezing `test_review_fixes_2.py` to rewrite its two constants, `CAPITAL_SIGMA` and `SIGMA`, because they used a non-reserved domain (`a@ΑΣ-b.gr`, `a@ασ-b.gr`), which breaks criterion 10. The proposed values `a@ΑΣ.example.org` / `a@ασ.example.org` were measured and rejected: "." is case-ignorable in Python's final-sigma rule, so `"a@ΑΣ.example.org".lower()` gives plain sigma `'a@ασ.example.org'` and the U1 test would pass against the defective whole-string `.lower()`. The constants are now `a@ΑΣ-b.example.org` / `a@ασ-b.example.org`: the "-" keeps the sigma word-final (whole-string `.lower()` gives `'a@ας-b.example.org'`), and the domain is reserved. Nothing else in the file changed. The user accepted these values. The user also chose to amend criterion 10 rather than rewrite the length-probe addresses built in code in `test_review_fixes_3.py` and `test_review_fixes_4.py`.

## Design

- `User` is the domain's **Account** (see `CONTEXT.md`): a registered OPEN login. The model keeps Django's conventional name `User` so `AUTH_USER_MODEL = "accounts.User"` reads as Django developers expect.
- `username = None`, `first_name = None`, `last_name = None`.
- `email = models.EmailField("email address", max_length=EMAIL_MAX_LENGTH, unique=True, validators=[validate_normalized_email])` (required, since `blank` defaults to false; `EMAIL_MAX_LENGTH` is 254, from `validators.py`). Case-insensitive uniqueness is a database constraint on the lowercased value: `models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_unique", violation_error_message="An account with this email address already exists.")`. This is the authoritative guard against case-variant duplicates, including writes that skip the manager (`save()` on a raw instance, `bulk_create`, `update`). It does not normalize: those paths still store a mixed-case or untrimmed value when no variant exists. Lookups compare `Lower(email)`, so such rows still log in.
- ASCII-only (round R3; since round R6 this rule lives in the unified validator below): `apps/accounts/validators.py` holds `normalize_email_address` (NFKC, strip, per-character lowercase; the manager's `normalize_email` delegates to it) and, until R6, `validate_ascii_email`, which raised `ValidationError("Email addresses may contain only ASCII characters.")` when the normalized value is not ASCII. As a field validator it reaches the admin add and change forms, `full_clean()`, and `createsuperuser` (interactive and `--noinput`, both call `field.clean`). It checks the normalized value because field validators run before `clean()` normalizes, so long s is accepted on every path. `_create_user` raises `ValueError` with the same message. The database guard is `models.CheckConstraint(condition=Q(email__regex=r"^[\x00-\x7F]*$"), name="accounts_user_email_ascii", violation_error_message=...)`, which stops raw `save()`, `bulk_create`, and `update` (verified on postgres:16-alpine: rejects `ınfo@example.org` and `a@ασ-b.gr`, accepts plain addresses). The `Lower` lookup, the non-string guard, and `aget_by_natural_key` stay as defense in depth.
- Normalized length (round R5; since round R6 this rule lives in the unified validator below): `max_length` checks the raw value, but NFKC can expand it (U+FB00 becomes "ff"), and `clean()` stores the expanded value, so a 213-character address could reach the database as 413 characters and fail with `DataError` (a 500 in the admin). `validators.py` holds the one limit, `EMAIL_MAX_LENGTH = 254`, which `User.email` also uses as `max_length`. A second field validator, `validate_normalized_email_length`, raises `ValidationError("Email addresses may be at most 254 characters.")` when the normalized value is longer, so the admin forms, `full_clean()`, and `createsuperuser` reject it cleanly, and `_create_user` raises `ValueError` with the same message. A normalized length of exactly 254 is accepted. The field's validator list changed, so the unshipped `0001_initial.py` was deleted and regenerated with `makemigrations accounts`.
- Unified validation (round R6): the single rule is "normalize, then validate the normalized value", because `clean()` and the manager store the normalized value while Django's `EmailValidator` (on `EmailField` and on the form field) checks only the raw one. NFKC can turn a valid raw address into a malformed one: `"a@" + "ﬀ" + "b" * 62 + ".com"` has a 63-character label raw and a 64-character label stored. `validators.py` now has:
  - `normalized_email_errors(normalized)`: returns every rule the normalized value breaks, in order: ASCII (`ASCII_EMAIL_MESSAGE`, code `email_not_ascii`), length (`EMAIL_LENGTH_MESSAGE`, code `email_too_long`), then Django's `validate_email` ("Enter a valid email address.", code `invalid`).
  - `validate_normalized_email(value)`: the one field validator. It normalizes, collects those errors into one `ValidationError` list, drops a format message that the raw `EmailValidator` already reports, and reports identical messages once. It replaces `validate_ascii_email` and `validate_normalized_email_length` on the field, and both were removed.
  - `_create_user` runs `normalized_email_errors` on the normalized email and raises `ValueError` with the first message, so the manager now also rejects malformed addresses (for example `"not an email"`). The ASCII and length `ValueError` messages are unchanged.
  - The validator list changed again, so `0001_initial.py` was regenerated (still the only migration).
- Changed during implementation: the plan first left out `unique=True`. Django ignores expression constraints when it decides whether `USERNAME_FIELD` is unique (`Options.total_unique_constraints` skips them). Without `unique=True`, `manage.py check` fails with `accounts.User: (auth.E003) 'User.email' must be unique because it is named as the 'USERNAME_FIELD'.` (measured), and `createsuperuser` skips its duplicate check (`username_is_unique` is false), so a case-variant duplicate would crash with IntegrityError. `unique=True` fixes both; the `Lower` constraint still guards case variants.
- `USERNAME_FIELD = "email"`, `EMAIL_FIELD = "email"`, `REQUIRED_FIELDS = []`.
- `UserManager(BaseUserManager["User"])` (subscripting is runtime-safe on Django 5.2.19; see round R4):
  - `normalize_email(email)` is overridden as a classmethod that applies NFKC (as `AbstractBaseUser.clean()` does), strips surrounding whitespace, and lowercases the whole address one character at a time; `None` becomes empty. (Changed after delta review: `str.lower()` on the whole string applies the final-sigma rule, so `a@ΑΣ-b.gr` became `a@ας-b.gr` while Postgres `LOWER()` gives `σ`; per-character lowercasing matches the database.) `AbstractUser.clean()` calls it, so model forms and `full_clean()` normalize too.
  - `create_user(email, password=None, **extra_fields)` and `create_superuser(email, password=None, **extra_fields)`: an empty or missing email raises `ValueError("An email address is required.")`, and a normalized email that breaks the ASCII, length, or format rule raises `ValueError` with that rule's message (rounds R3, R5, R6). `create_superuser` forces `is_staff=True` and `is_superuser=True` and raises `ValueError` if either is passed as false, like Django's own manager.
  - `get_by_natural_key(email)` and `aget_by_natural_key(email)` look up with the constraint's own expression, `alias(email_lower=Lower("email")).get(email_lower=Lower(Value(normalize_email(email))))`, so at most one row can match. Login (sync and async) and `createsuperuser`'s duplicate check are case-insensitive and ignore surrounding whitespace. A non-string identifier (for example an int or a list) returns `self.none()`, so login fails with `DoesNotExist` instead of raising `TypeError`. (Changed after review: the plan first used `email__iexact`, see Risks.)
- `User.get_full_name()` returns `""` and `get_short_name()` returns the email, since `first_name` and `last_name` are gone (the inherited method returned the string "None None", shown in the admin history page).
- Admin: a `UserAdmin` subclass registered for `User`, with forms keyed on email:
  - In `apps/accounts/forms.py`, `UserCreationForm` subclasses Django's `AdminUserCreationForm` (which carries the 5.2 `usable_password` field) and overrides only `Meta`, inheriting the parent `Meta`: `model = User`, `fields = ("email",)`, `field_classes = {}`.
  - `UserChangeForm` subclasses Django's `UserChangeForm`, overriding only `Meta`: `model = User`, `fields = "__all__"`, `field_classes = {}`.
  - `fieldsets`: `(None, {"fields": ("email", "password")})`, Permissions (`is_active`, `is_staff`, `is_superuser`, `groups`, `user_permissions`), Important dates (`last_login`, `date_joined`).
  - `add_fieldsets`: `("email", "usable_password", "password1", "password2")` with `"classes": ("wide",)`.
  - `list_display = ("email", "is_staff", "is_active")`, `search_fields = ("email",)`, `ordering = ("email",)`.
- The module docstring of `apps/accounts/__init__.py` (and the `User` class docstring) says: this app holds the Account login model; player profile data (name, ratings, matches, external IDs) does not live on `User` but on Player.

## Acceptance criteria

1. `uv run python manage.py migrate` on an empty Postgres database succeeds, and `accounts.0001_initial` is applied before `admin.0001_initial`.
2. `uv run python manage.py createsuperuser` prompts for "Email address", and no prompt mentions "Username" or "username".
3. Two users whose emails differ only in case cannot be created: the database rejects it (IntegrityError) even when the manager is bypassed, and `createsuperuser`, `full_clean()`, and the admin add form each reject it with a validation error rather than a crash.
4. `settings.AUTH_USER_MODEL == "accounts.User"`, `apps.accounts` is in `INSTALLED_APPS`, and `get_user_model()` is `apps.accounts.models.User`.
5. `User` has no `username`, `first_name`, or `last_name` field; `email` is required.
6. Login is case-insensitive on email: `authenticate(email="Ann@Example.org" variant)` finds the user created as `ann@example.org`; the manager stores emails lowercased.
7. `User` is registered in the admin; the changelist, add, and change views render (200) for a superuser, and the add form creates a user by email.
8. `apps/accounts` documents that player profile data does not live on `User`.
9. Exactly one migration exists in the repository: `apps/accounts/migrations/0001_initial.py`. `makemigrations --check --dry-run` reports no changes.
10. No real player data, no emojis, and no em dashes in any added or changed file. Literal test emails use reserved domains (`example.org`, `example.com`, or a subdomain of them). Addresses built in code to probe length or label limits (in `test_review_fixes_3.py` and `test_review_fixes_4.py`) may use generated domains; they cannot be real data. (Amended after the Phase 7 verifier, with the user's approval.)
11. Email addresses are ASCII-only after NFKC normalization, rejected by the field validator, the manager, and a database check constraint. They are also at most 254 characters after normalization (round R5), and the normalized value must be a valid email address (round R6); one field validator, `validate_normalized_email`, and the manager enforce all three rules on the normalized value.

## Files that change

New:
- `apps/accounts/__init__.py` (docstring)
- `apps/accounts/apps.py` (`AccountsConfig`, `name = "apps.accounts"`, label defaults to `accounts`, `verbose_name = "Accounts"`)
- `apps/accounts/models.py` (`UserManager`, `User`)
- `apps/accounts/forms.py` (creation and change forms)
- `apps/accounts/validators.py` (`normalize_email_address`, `ASCII_EMAIL_MESSAGE`; round R3. `EMAIL_MAX_LENGTH`, `EMAIL_LENGTH_MESSAGE`; round R5. `normalized_email_errors` and the single field validator `validate_normalized_email`; round R6, replacing R3's `validate_ascii_email` and R5's `validate_normalized_email_length`)
- Round R4: type annotations in `apps/accounts/models.py`, `validators.py`, and `forms.py` (no behavior change; migrations and tests untouched)
- `apps/accounts/admin.py` (`UserAdmin`)
- `apps/accounts/migrations/__init__.py`, `apps/accounts/migrations/0001_initial.py` (generated by `makemigrations accounts`, never hand-written)
- `apps/accounts/tests/__init__.py`, `apps/accounts/tests/test_models.py`, `apps/accounts/tests/test_createsuperuser.py`, `apps/accounts/tests/test_admin.py`
- Frozen regression tests from the review rounds: `apps/accounts/tests/test_review_fixes.py` (T1 to T7; its `LookupMatchesConstraintTests` rewritten in R3 with user approval), `apps/accounts/tests/test_review_fixes_2.py` (U1, U2; its `FinalSigmaTests` rewritten in R3 with user approval), `apps/accounts/tests/test_ascii_policy.py` (R3), `apps/accounts/tests/test_review_fixes_3.py` (R5, V1), `apps/accounts/tests/test_review_fixes_4.py` (R6, W1)
- `docs/plans/2026-11-10-issue-3-add-a-custom-user-model-before-the-first.md` (this plan)

Changed:
- `config/settings.py`: add `"apps.accounts"` to `INSTALLED_APPS`; add `AUTH_USER_MODEL = "accounts.User"`; replace the comment saying the custom user model must land before migrate with one saying `AUTH_USER_MODEL` is fixed and the OAuth Application model (#22) must land before django-oauth-toolkit's migrations run.
- Phase 8 (coordinator, not the implementer): `README.md`, `CLAUDE.md`, `CONTEXT.md`, and `docs/adr/README.md` where their text becomes wrong, plus a new record, `docs/adr/0003-email-identity-for-accounts.md` (0001 and 0002 are reserved for issues #16 and #18).

Not created: `conftest.py` or pytest config (#12), `docker-compose.yml` (#7), ruff or mypy config (#11), any other app or migration.

## Order of work

Run every command from the worktree root. Do not commit or push: this run is in no-commit mode, so leave every file uncommitted.

Postgres: until #7 lands, run a throwaway container for the whole implementation and remove it at the end:
`docker run -d --rm --name open-issue3-pg -p 127.0.0.1:5432:5432 -e POSTGRES_USER=open -e POSTGRES_DB=open -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine`, then wait for `docker exec open-issue3-pg pg_isready -U open`. Never run `migrate` against it before step 4 has set `AUTH_USER_MODEL`.

1. Create the app skeleton: `apps/accounts/__init__.py`, `apps.py`, an empty `models.py`, and `tests/__init__.py`. Add `"apps.accounts"` to `INSTALLED_APPS`. `manage.py check` passes.
2. Red: write all tests in `apps/accounts/tests/` (see Proof). Run `uv run python manage.py test apps.accounts` and save the failing output (expected: import errors or failures because `User` does not exist and `AUTH_USER_MODEL` is unset). Record that output in the implementer report.
3. Write `UserManager` and `User` in `models.py`.
4. Set `AUTH_USER_MODEL = "accounts.User"` and update the settings comment.
5. `uv run python manage.py makemigrations accounts`. Read the generated `0001_initial.py`: it must create `User` with the `Lower("email")` constraint, without `username`, `first_name`, or `last_name`, and depend on `auth`. Do not edit it by hand.
6. Write `forms.py` and `admin.py`.
7. Green: run the gate below until all of it passes, then the live drive.
8. Round R3: ASCII-only emails. Rewrite the six user-approved frozen tests and add `test_ascii_policy.py`; record the red run; freeze them in `.claude/fix-lock`; add the field validator, the manager check, and the `accounts_user_email_ascii` check constraint; delete and regenerate `0001_initial.py`; remove the lock when green. Then the full gate from an empty database and a live drive (interactive `createsuperuser` with a non-ASCII address first, a raw-SQL check-constraint probe, admin add POST with a non-ASCII address, case-variant admin login).
9. Round R4: add type annotations to the accounts production code, then rerun the full gate from an empty database, an advisory `uvx mypy==1.18.2 apps/accounts --ignore-missing-imports`, and a short live drive (case-variant admin login, `createsuperuser --noinput`).
10. Round R5: normalized length. Freeze `test_review_fixes_3.py` red first, add `EMAIL_MAX_LENGTH` and the length check (field validator and manager), regenerate `0001_initial.py`, remove the lock when green. Then the full gate and a live drive (admin add POST with an address that expands past 254, case-variant admin login).
11. Round R6: validate the normalized value. Freeze `test_review_fixes_4.py` red first, replace the field validators with `validate_normalized_email` (ASCII, length, and format on the normalized value) and use the same rules in the manager, regenerate `0001_initial.py`, remove the lock when green. Repair the plan. Then the full gate and a live drive (admin add POST with an address whose normalized form is malformed, admin add POST with a non-ASCII address, interactive `createsuperuser` with the malformed address first, case-variant admin login).

## Gate (derived; pytest, ruff, and mypy arrive in #11 and #12)

Run in this order and keep the output:

1. `uv lock --check`
2. `uv run python manage.py check`
3. `uv run python manage.py makemigrations --check --dry-run` (expect "No changes detected")
4. `find . -path ./.venv -prune -o -type d -name migrations -print` (expect only `./apps/accounts/migrations`) and `ls apps/accounts/migrations` (expect `__init__.py` and `0001_initial.py` only, ignoring `__pycache__`)
5. Empty-database migrate: `docker exec open-issue3-pg psql -U open -d postgres -c "DROP DATABASE IF EXISTS open" -c "CREATE DATABASE open"`, then `uv run python manage.py migrate`, then `uv run python manage.py showmigrations --plan` (accounts.0001_initial listed before admin.0001_initial, all applied)
6. `uv run python manage.py test apps --verbosity 2` (all pass; test database `test_open` is created and destroyed by the runner)
7. `grep -rnP "\x{2014}|[\x{1F300}-\x{1FAFF}\x{2600}-\x{27BF}]" apps config docs README.md CLAUDE.md` (expect no output)
8. Advisory, not blocking (ruff is not a project dependency until #11): `uvx ruff@0.17.4 check . && uvx ruff@0.17.4 format --check .`

## Live drive

High tier: drive the changed path and its failure and edge paths, against the real server and the migrated database from gate step 5.

1. Interactive `createsuperuser` in a real pseudo-terminal (it refuses to prompt without a TTY). Drive it with a short script in the scratch directory using the standard library `pty` module, or `script -qec`. Show the full transcript: the first prompt is "Email address:", no prompt says "Username", and a password is accepted. Use `Admin.Drive@Example.org` and confirm it is stored as `admin.drive@example.org`.
2. Run `createsuperuser` again interactively and enter `ADMIN.DRIVE@example.org` first: the transcript shows the duplicate error, then a second, new address succeeds.
3. `uv run python manage.py runserver 127.0.0.1:8765` in the background. With `curl` and a cookie jar:
   - GET `/admin/login/` (200), POST the login form (CSRF token from the cookie, form field `username` carries the email) with `Admin.Drive@EXAMPLE.org` and the right password: redirect to `/admin/`.
   - Same login with a wrong password: the page shows the login error, no session.
   - GET `/admin/accounts/user/` (200, lists the superuser's email), GET `/admin/accounts/user/add/` (200, has an `email` input and no `username` input).
   - POST the add form with `new.player.account@example.org` and a valid password pair: redirect, and the user exists (check with `manage.py shell -c`).
   - POST the add form with `NEW.Player.Account@example.org`: 200 with the "already exists" message, and the user count is unchanged.
4. Stop the server and run `docker stop open-issue3-pg`.

## Risks

- Riskiest step: the initial migration. If any other migration runs or is generated before `AUTH_USER_MODEL` is set, Django's `auth.User` table and admin's foreign key to it become part of history. Mitigation: settings and the migration change together; the gate starts from an empty database and checks the migration list.
- Case handling disagreement (fixed after review): the constraint uses `Lower`, while `email__iexact` on Postgres compares `UPPER`. They disagree for some Unicode characters: `info@example.org` and `ınfo@example.org` (U+0131 dotless i) are distinct under `Lower` but equal under `UPPER`, so both rows could exist and an `iexact` login raised `MultipleObjectsReturned`. Fix: the natural-key lookups compare `Lower(email)` with `Lower(normalized input)`, the constraint's own expression, so at most one row can match. The manager also applies NFKC so `inſo@example.org` (U+017F long s) and `inso@example.org` are one address, as on the form path.
- Internationalized email addresses (non-ASCII local parts or Unicode domains) are deliberately unsupported for now (round R3, user decision): ASCII-only blocks lookalike accounts at every layer. Allowing them later needs a homoglyph strategy (for example a confusables skeleton), a validator change, and a migration that relaxes `accounts_user_email_ascii`.
- Lowercasing the local part: technically case-sensitive per RFC 5321. Accepted, so that case variants cannot create separate Accounts.
- Admin forms in 5.2 carry `usable_password` and inherit `Meta.field_classes = {"username": UsernameField}`. Mitigation: override `Meta` with `field_classes = {}` and test the add view.
- `UserAttributeSimilarityValidator` lists `username`, `first_name`, `last_name`; it skips attributes the user lacks, so removing them is safe. The admin add test with a password similar to the email proves the validator still runs.
- A throwaway container on port 5432 could collide with a local Postgres. Checked before planning: nothing listens on 5432.

Choices not taken: pytest now (#12), a citext column (needs a Postgres extension migration; the functional constraint is enough), dropping `unique=True` in favor of the constraint alone (fails `auth.E003`, see Design), naming the model `Account` (the issue and Django convention say `User`; the docstring maps it to Account).

## Proof

| Criterion | Proven by |
| --- | --- |
| 1 | Gate 5; also every test run, since the runner migrates `test_open` from empty |
| 2 | `test_createsuperuser.py`: patches `createsuperuser.input` (with `create=True`) and `getpass.getpass`, records every prompt, asserts the first starts with "Email address" and none contains "username" (case-insensitive); live drive 1 |
| 3 | `test_models.py`: `create_user` twice with case variants raises `IntegrityError`; with `ann@example.org` stored, `User(email="Ann@Example.org").save()` (manager bypassed, no normalization) raises `IntegrityError`; `full_clean()` on a case variant raises `ValidationError` mentioning the email; `test_createsuperuser.py`: interactive duplicate re-prompts with an error; `test_admin.py`: add form POST with a case variant shows the error and creates nothing; live drive 2 and 3 |
| 4 | `test_models.py`: settings and `get_user_model()` assertions |
| 5 | `test_models.py`: `User._meta.get_field` raises `FieldDoesNotExist` for each removed field; `create_user("")` raises `ValueError`; `full_clean()` with blank email raises `ValidationError` |
| 6 | `test_models.py`: stored email is lowercased; `authenticate(None, username="ANN@Example.ORG", password=...)` returns the user; `get_by_natural_key` with a case variant returns the user |
| 7 | `test_admin.py`: `admin.site.is_registered(User)`; changelist, add, change views 200 for a superuser via `force_login`; add POST creates a user; live drive 3 |
| 8 | `test_models.py`: `"player" in apps.accounts.__doc__.lower()` and `"profile"` present; plus reading the docstring |
| 9 | Gate 3, gate 4 |
| 10 | Gate 7; reading the diff |
| 11 | `test_ascii_policy.py`: `full_clean()` field validator; manager `ValueError` for `create_user` and `create_superuser`; check constraint `IntegrityError` on raw `save()`, `bulk_create`, and `update`; admin add and change forms show the message and change nothing; interactive `createsuperuser` re-prompts with the message; long s folds to `inso@example.org` and is accepted (manager, `full_clean()`, admin add form); the rewritten tests in `test_review_fixes.py` and `test_review_fixes_2.py` (dotless i and Greek sigma rejected, `--noinput` ASCII error, no lookup crash); live drive R3; `test_review_fixes_3.py` (round R5): an address that NFKC expands past 254 characters is rejected by `full_clean()` (`ValidationError` with the length message), `create_user` (`ValueError`), `createsuperuser --noinput` (`CommandError`), and the admin add form (200 with the message, nothing created); a normalized length of exactly 254 is accepted; live drive R5; `test_review_fixes_4.py` (round R6, the unified validator `validate_normalized_email`): an address valid raw but malformed after NFKC is rejected by `full_clean()` ("Enter a valid email address."), `create_user` (`ValueError`), `createsuperuser --noinput` (`CommandError`), and the admin add form (200 with the message, nothing created); `create_user("not an email")` raises `ValueError`; a raw-malformed address is reported once; long s is still accepted; live drive R6 |

Live drive must show: the interactive prompt transcript, the duplicate rejection, an admin login with a case variant, a failed login, and admin add success and duplicate rejection.

Measured on the default branch (5471951), for the "phrase absent" test of criterion 2:

```
$ uv run python -c "...; U=get_user_model(); print(U._meta.label, U.USERNAME_FIELD, repr(capfirst(U._meta.get_field(U.USERNAME_FIELD).verbose_name)))"
auth.User username 'Username'
```

So the prompt test fails on the default branch for the right reason (the prompt is "Username"), and the absence of "username" is a real change, not a vacuous check.

Red run (step 2, no-commit mode), `uv run python manage.py test apps.accounts` before `User` existed, summarized:

```
ImportError: cannot import name 'User' from 'apps.accounts.models'
ERROR: test_add_form_creates_user_by_email ... (and the other 5 admin tests)
ERROR: test_prompts_for_email_and_never_username: StopIteration
  (auth.User prompts "Username" first, so the scripted answers run out)
FAIL: test_duplicate_case_variant_is_rejected_then_reprompted
AssertionError: 'already taken' not found in ''
Ran 10 tests in 0.361s
FAILED (failures=1, errors=8)
```

One test was corrected while going green (not a defect test, never frozen): `test_full_clean_normalizes_email` first used `"  Bea@Example.COM "`. `full_clean()` runs `clean_fields()` before `clean()`, so the raw value with spaces fails `EmailField` validation before normalization. The test now uses `"Bea@Example.COM"`; stripping whitespace is covered by `test_create_user_stores_email_lowercased`.

Live drive note: because `email` has `unique=True` and the `Lower` constraint (see Design), the admin add form shows two errors for a case-variant duplicate: "An account with this email address already exists." and "User with this Email address already exists." Both reject the write; the user count was unchanged. The server ran with `--noreload` so one process could be stopped cleanly.

### Review fixes (T1 to T7), frozen test `apps/accounts/tests/test_review_fixes.py`

Red run, `uv run python manage.py test apps.accounts.tests.test_review_fixes -v 2`, before the fixes (postgres:16-alpine):

```
test_aauthenticate_is_case_insensitive ... FAIL
test_createsuperuser_noinput_rejects_case_variant ... ok          (T7b, coverage)
test_full_clean_hits_the_lower_constraint ... ok                  (T7a, coverage)
test_queryset_update_to_case_variant_raises_integrity_error ... ok (T7c, coverage)
test_authenticate_ascii_address_returns_its_own_user ... ERROR
test_authenticate_dotless_address_returns_its_own_user ... ERROR
test_createsuperuser_noinput_reports_duplicate_instead_of_crashing ... ERROR
test_get_by_natural_key_returns_the_ascii_user ... ERROR
test_authenticate_with_surrounding_whitespace ... FAIL
test_get_full_name_is_empty ... FAIL
test_get_short_name_is_the_email ... FAIL
test_manager_stores_nfkc_normalized_email ... FAIL
test_nfkc_equivalent_address_is_a_duplicate ... FAIL
T1 (all 4 ERRORs): apps.accounts.models.User.MultipleObjectsReturned: get() returned more than one User -- it returned 2!
T2: AssertionError: None != <User: ann@example.org>
T3: AssertionError: None != <User: bob@example.org>
T5: AssertionError: 'None None' != ''
T5: AssertionError: None != 'ann@example.org'
T4: AssertionError: 'inſo@example.org' != 'inso@example.org'
T4: AssertionError: IntegrityError not raised
Ran 13 tests in 1.145s
FAILED (failures=6, errors=4)
```

Green after the fixes: `uv run python manage.py test apps` ran 44 tests, OK. T7 (a), (b), and (c) passed on first run: they are coverage for behavior that already worked, not defects. `.claude/fix-lock` was removed once green.

### Delta-review fixes (U1, U2), frozen test `apps/accounts/tests/test_review_fixes_2.py`

Red run, `uv run python manage.py test apps.accounts.tests.test_review_fixes_2 -v 2`, before the fixes (postgres:16-alpine):

```
test_authenticate_with_sigma_variant_finds_the_account ... FAIL
test_sigma_variant_is_a_duplicate ... FAIL
test_aauthenticate_with_int_returns_none ... ERROR
test_aauthenticate_with_list_returns_none ... ERROR
test_authenticate_with_int_returns_none ... ERROR
test_authenticate_with_list_returns_none ... ERROR
U2: TypeError: normalize() argument 2 must be str, not int   (sync and async)
U2: TypeError: normalize() argument 2 must be str, not list  (sync and async)
U1: AssertionError: None != <User: a@ας-b.gr>
U1: AssertionError: IntegrityError not raised
Ran 6 tests in 0.437s
FAILED (failures=2, errors=4)
```

Green after the fixes: `uv run python manage.py test apps` ran 50 tests, OK. `.claude/fix-lock` was removed once green.

### Round R3: ASCII-only email addresses (criterion 11)

Frozen tests: `apps/accounts/tests/test_ascii_policy.py` (new), plus the six user-approved rewrites in `test_review_fixes.py` (`LookupMatchesConstraintTests`) and `test_review_fixes_2.py` (`FinalSigmaTests`). The merge-base has no accounts app, so a scratch worktree there cannot run these tests. The red run below, against this branch before the R3 production change, is the proof for the rewritten tests and the new file.

Rewritten test names (old to new): `test_authenticate_dotless_address_returns_its_own_user` to `test_authenticate_dotless_address_returns_none`; `test_authenticate_with_sigma_variant_finds_the_account` to `test_capital_sigma_lowers_per_character_and_is_rejected`; `test_sigma_variant_is_a_duplicate` to `test_sigma_variant_is_rejected`. The other three keep their names.

Red run, `uv run python manage.py test apps.accounts.tests.test_ascii_policy apps.accounts.tests.test_review_fixes.LookupMatchesConstraintTests apps.accounts.tests.test_review_fixes_2.FinalSigmaTests -v 2` (postgres:16-alpine):

```
test_add_form_accepts_long_s_and_folds_it ... ok
test_add_form_rejects_non_ascii_email ... FAIL            AssertionError: 302 != 200
test_change_form_rejects_non_ascii_email ... FAIL         AssertionError: 302 != 200
test_bulk_create_of_non_ascii_email_raises_integrity_error ... FAIL   IntegrityError not raised
test_raw_save_of_ascii_email_is_accepted ... ok
test_raw_save_of_non_ascii_email_raises_integrity_error ... FAIL      IntegrityError not raised
test_update_to_non_ascii_email_raises_integrity_error ... FAIL        IntegrityError not raised
test_interactive_reprompts_with_ascii_message ... FAIL    'Email addresses may contain only ASCII characters.' not found in ''
test_full_clean_accepts_long_s_and_folds_it ... ok
test_full_clean_rejects_non_ascii_email ... FAIL          ValidationError not raised
test_create_superuser_rejects_non_ascii_email ... FAIL    ValueError not raised
test_create_user_accepts_long_s_and_folds_it ... ok
test_create_user_rejects_non_ascii_email ... FAIL         ValueError not raised
test_authenticate_ascii_address_returns_its_own_user ... ok
test_authenticate_dotless_address_returns_none ... FAIL   IntegrityError not raised
test_createsuperuser_noinput_reports_duplicate_instead_of_crashing ... FAIL   CommandError not raised (ASCII message)
test_get_by_natural_key_returns_the_ascii_user ... FAIL   ValueError not raised
test_capital_sigma_lowers_per_character_and_is_rejected ... FAIL      ValueError not raised
test_sigma_variant_is_rejected ... FAIL                   ValueError not raised
Ran 19 tests in 1.545s
FAILED (failures=14)
```

The 5 passing tests cover behavior that already worked (long-s folding, plain ASCII raw save, ASCII login) and guard against the policy over-reaching.

Green after the R3 change: `uv run python manage.py test apps` ran 63 tests, OK. `0001_initial.py` was deleted and regenerated with `makemigrations accounts` (not hand-edited); it is still the only migration and now carries the field validator and the `accounts_user_email_ascii` check constraint. `.claude/fix-lock` was removed once green; the three test files stay frozen.

Live drive R3 (full gate passed first from an empty database, 63 tests OK):

- Interactive `createsuperuser` in a pseudo-terminal, entering `ınfo@example.org` first:

  ```
  Email address: ınfo@example.org
  Error: Email addresses may contain only ASCII characters.
  Email address: Admin.Drive@Example.org
  Password:
  Password (again):
  Superuser created successfully.
  ```

  Only `admin.drive@example.org` was stored.
- Raw SQL through `psql`, inserting `ınfo@example.org` into `accounts_user`: `ERROR: new row for relation "accounts_user" violates check constraint "accounts_user_email_ascii"`.
- Over HTTP: login as `ADMIN.drive@example.ORG` gave 302 to `/admin/`; the add-form POST with `ınfo@example.org` gave 200 with "Email addresses may contain only ASCII characters."; the user count stayed 1.

### Round R4: type annotations

The user asked for type hints on the accounts production code (see Decisions). No behavior change, so no frozen test; no frozen test file was edited.

- Full gate from an empty database: all 8 steps passed; `Ran 63 tests`, OK. The `ClassVar` annotations cleared the four RUF012 findings in production code, so `ruff check .` dropped from 7 to 3 findings, all in the generated migration.
- Advisory `uvx mypy==1.18.2 apps/accounts --ignore-missing-imports` (no project config): `Success: no issues found in 15 source files`. Without django-stubs (#11), Django types resolve to `Any`, so this only checks consistency inside the app.
- Live drive R4: `DJANGO_SUPERUSER_PASSWORD=... manage.py createsuperuser --noinput --email 'Admin.Drive@Example.org'` printed "Superuser created successfully."; the same command with `ADMIN.DRIVE@example.org` printed `CommandError: Error: That email address is already taken.`; admin login over HTTP as `Admin.Drive@EXAMPLE.org` gave 302 to `/admin/` and the index (200) greeted `admin.drive@example.org`.

### Round R5: normalized length (V1), frozen test `apps/accounts/tests/test_review_fixes_3.py`

NFKC can expand an address past 254 characters after the raw value passed `MaxLengthValidator`: `"a@" + ".".join(["ﬀ" * 25] * 8) + ".com"` (U+FB00, 213 raw characters) normalizes to 413.

Red run, `uv run python manage.py test apps.accounts.tests.test_review_fixes_3 -v 2`, before the fix (postgres:16-alpine):

```
test_add_form_rejects_address_that_expands_past_254 ... ERROR
test_create_user_rejects_address_that_expands_past_254 ... ERROR
test_createsuperuser_noinput_reports_length_error ... ERROR
test_full_clean_rejects_address_that_expands_past_254 ... FAIL
test_lengths_of_the_fixtures ... ok
test_normalized_length_of_exactly_254_is_accepted ... ok
ERROR (admin add, create_user, createsuperuser): django.db.utils.DataError: value too long for type character varying(254)
FAIL (full_clean): AssertionError: ValidationError not raised
Ran 6 tests in 0.716s
FAILED (failures=1, errors=3)
```

The 2 passing tests check the fixtures themselves and that a normalized length of exactly 254 (plain ASCII, and a 253-character address that expands to 254) stays accepted, so the fix cannot over-reach.

Green after the R5 fix: `uv run python manage.py test apps` ran 69 tests, OK. `0001_initial.py` was regenerated (still the only migration; the email field now lists both validators, `max_length` stays 254). `.claude/fix-lock` was removed once green.

Live drive R5 (full gate passed first from an empty database, 69 tests OK): over HTTP, login as `ADMIN.drive@example.ORG` gave 302 to `/admin/`; the add-form POST with the 213-character U+FB00 address gave 200 with "Email addresses may be at most 254 characters."; the user count stayed 1; the server log had no 500 responses (every request 200, the login POST 302).

### Round R6: validate the normalized value (W1), frozen test `apps/accounts/tests/test_review_fixes_4.py`

Django's `EmailValidator` checked only the raw value, and `clean()` stores the NFKC-normalized value. `"a@" + "ﬀ" + "b" * 62 + ".com"` has a valid 63-character first domain label raw and an invalid 64-character label after NFKC; `full_clean()` accepted it and `validate_email()` on the stored value raised "Enter a valid email address." (measured).

Red run, `uv run python manage.py test apps.accounts.tests.test_review_fixes_4 -v 2`, before the fix (postgres:16-alpine):

```
test_add_form_rejects_address_whose_normalized_form_is_malformed ... FAIL   AssertionError: 302 != 200
test_create_user_rejects_address_whose_normalized_form_is_malformed ... FAIL   ValueError not raised
test_create_user_rejects_malformed_address ... FAIL   ValueError not raised
test_createsuperuser_noinput_rejects_it ... FAIL   CommandError not raised
test_fixture_label_lengths ... ok
test_full_clean_rejects_address_whose_normalized_form_is_malformed ... FAIL   ValidationError not raised
test_long_s_address_is_still_accepted ... ok
test_raw_malformed_address_is_reported_once ... ok
Ran 8 tests in 0.702s
FAILED (failures=5)
```

The 3 passing tests are guards: the fixture really expands to a 64-character label, long s stays accepted, and a raw-malformed address is reported exactly once (no duplicate message after the fix).

Before the fix, every existing `create_user` call in the tests was checked: all pass well-formed addresses or expect an error whose message comes first in the new order (empty, then ASCII, then length, then format), so no frozen test changed. Green after the R6 fix: `uv run python manage.py test apps` ran 77 tests, OK. `0001_initial.py` was regenerated; it is still the only migration and the email field lists only `validate_normalized_email`. `.claude/fix-lock` was removed once green.

Live drive R6 (full gate passed first from an empty database, 77 tests OK):

- Interactive `createsuperuser` in a pseudo-terminal, entering `"a@" + "ﬀ" + "b" * 62 + ".com"` first:

  ```
  Email address: a@ﬀbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.com
  Error: Enter a valid email address.
  Email address: Admin.Drive@Example.org
  Password:
  Password (again):
  Superuser created successfully.
  ```

  Only `admin.drive@example.org` was stored.
- Over HTTP: login as `ADMIN.drive@example.ORG` gave 302 to `/admin/`. The add-form POST with the same address gave 200 with "Enter a valid email address." (shown once). The add-form POST with `ınfo@example.org` gave 200 with "Email addresses may contain only ASCII characters." (shown once). The user count stayed 1, and the server log had no 500 responses.

### Round R7: reserved domain in `test_review_fixes_2.py` (criterion 10)

Only the two constants changed (user-approved, see Decisions): `CAPITAL_SIGMA = "a@ΑΣ-b.example.org"`, `SIGMA = "a@ασ-b.example.org"`. No production code changed; the file is frozen again.

Final-sigma check (`PYTHONDONTWRITEBYTECODE=1`, whole-string `.lower()` versus per-character):

```
'a@ΑΣ.example.org'   whole: 'a@ασ.example.org'    plain sigma (not usable)
'a@ΑΣ-b.example.org' whole: 'a@ας-b.example.org'  final sigma | per-character: 'a@ασ-b.example.org'
```

Proof that the rewritten test still catches the U1 defect: a scratch copy of the worktree (no `.venv`, `.git`, or caches) with `normalize_email_address` reverted to whole-string `email.lower()`, run with the worktree's venv against the throwaway Postgres, `manage.py test apps.accounts.tests.test_review_fixes_2.FinalSigmaTests -v 2`:

```
=== constants: CAPITAL_SIGMA=a@ΑΣ.example.org SIGMA=a@ασ.example.org
test_capital_sigma_lowers_per_character_and_is_rejected ... ok
test_sigma_variant_is_rejected ... ok
Ran 2 tests in 0.103s
OK                                   (vacuous: the defect goes undetected)
=== constants: CAPITAL_SIGMA=a@ΑΣ-b.example.org SIGMA=a@ασ-b.example.org
test_capital_sigma_lowers_per_character_and_is_rejected ... FAIL
test_sigma_variant_is_rejected ... ok
AssertionError: 'a@ας-b.example.org' != 'a@ασ-b.example.org'
Ran 2 tests in 0.018s
FAILED (failures=1)                  (caught: final sigma U+03C2)
```

The scratch copy was deleted afterwards. No live drive was needed beyond the gate, because no production code changed.
