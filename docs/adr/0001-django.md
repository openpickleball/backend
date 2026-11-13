# 0001. Why Django

- Status: Proposed
- Date: 2026-11-12

## Context

Contributors will ask why the backend uses Django (issue #16). This record answers once.

The rating math lives in the engine, `openpickleball-engine` (imported as `open_engine`). It is a pure Python package with no runtime dependencies, and it requires Python 3.10 or later. The backend must call the engine, not reimplement it (hard rule 2 in `CLAUDE.md`). The same engine code also runs the backtests: calibration on Match history and historical Replays of the Ledger.

OPEN also needs a back office. Staff and volunteers will verify Organizers, review Flagged Matches, and handle identity questions. These are security-sensitive tasks, and they need working screens early.

OPEN is run by volunteers. The framework should be one that many people already know, so that new contributors can help without learning a niche tool first.

## Decision

- The backend is written in Python 3.13 and uses Django.
- The engine is imported in-process. The API, the scheduled Replays, and the backtests all run the same engine code, with no service boundary and no port of the math.
- Django's admin is the first back office for moderators. Custom screens come later, only where the admin is not enough.
- Django is pinned to the 5.2 LTS line (`Django>=5.2,<5.3`, decision Q1). django-celery-beat pins `Django<6.1`, and 5.2 LTS has security support until April 2028.
- The rest of the stack builds on Django: django-ninja for the API at `/api/v1/` with OpenAPI docs, Postgres through psycopg 3, Celery with django-celery-beat on Valkey, django-allauth for accounts, and django-oauth-toolkit for partner API tokens (decision Q2).

## Consequences

- Easier: one language for the API and the engine. A Replay in the backend and a backtest on a laptop call the same functions.
- Easier: moderators get list, search, and edit screens from the admin without a separate frontend.
- Easier: Django's ORM, migrations, auth, and admin are mature and widely documented, so new contributors can find help.
- Harder: Django's own views do not give a typed API or OpenAPI docs. django-ninja supplies them. The backend does not use Django REST Framework.
- Harder: the ORM is synchronous by default. Slow work, such as Replays, email, and webhooks, runs in Celery workers, not in request handlers.
- Harder: some choices must be made before the first migrations. The custom user model (issue #3) and the custom OAuth Application model (issue #22) land before other app migrations (decision Q3).
- Harder: the admin can change records directly. It needs care around the append-only Ledger and other security-sensitive areas, so admin access to those records must be limited.
- The engine is a demonstration package, pinned as a git dependency on a tag (decision Q4). Its output stays labeled as demonstration ratings.
- Revisit: the move from 5.2 LTS to the next LTS, once it is released and the dependencies support it (issue #69).

## Alternatives considered

- **FastAPI.** It gives typed endpoints and OpenAPI docs out of the box, and it runs the engine in-process too. It has no built-in admin, ORM, migrations, or auth, so we would assemble and maintain those ourselves. django-ninja gives a similar typed API style on top of Django.
- **Flask.** It is small and well known, but it leaves the same gaps as FastAPI: no admin, ORM, migrations, or auth without extra packages.
- **A non-Python stack.** The engine is Python. Another language would need the engine behind a service boundary, or a port of the rating math. A port breaks hard rule 2, and a service boundary adds a network hop and a second deployment between the API and the backtests.
