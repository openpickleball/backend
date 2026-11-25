# 0033. Audit access changes from the admin and services, with an explicit actor

- Status: Accepted
- Date: 2026-11-24

## Context

Organizer Members, Club Roles, Club Organizer Grants, and Organizer Application Grants decide who may submit counted matches (ADR 0021, CLAUDE.md hard rule 8). Until #158, only Django's admin log recorded their changes, and #116 clears it. Other Audit Events are written through `record_event()` (ADR 0007) by services that take an explicit `actor`. These rows have no service yet: the admin saves them through inlines and its own add, change, and delete views, and they also go by cascade when a Club, Organizer, Partner Application, or Account is deleted.

## Decision

- **The admin and services record, with an explicit actor.** `apps.audit.access` has `save_access_row()`, `delete_access_row()`, and `record_cascaded_removals()`, each taking `actor`. Each records in the same transaction as the change, and a removal before the delete. A save that changes nothing, and a change that is refused, record nothing.
- **The admin goes through them.** `AccessAuditAdminMixin` routes `save_model`, `delete_model`, `delete_queryset` (so `delete_selected`), and the inline formsets of the Club, Organizer, Partner Application, Account, and grant admins through those functions, with `request.user` as actor.
- **Admin deletes record their cascades.** Deleting a Club, Organizer, Partner Application, or Account in the admin records one removal for each Organizer Member, Club Role, and grant the delete cascades to, before the delete. The rows come from collecting the delete as Django will run it.
- **An Account deleting itself is not the actor of its own removals.** An event naming it would point at a row gone at commit and fail the whole delete. The event has no actor, and its `before` holds the Account's row id.
- **Values.** `before` and `after` hold the role, OPEN Codes, the Account's row id, and the application's `client_id`. Never an email or an Organizer's name (ADR 0009). An Account has no OPEN Code, and its email is identifying, so the row id is the only identifier left.
- **Out of scope.** A plain ORM save or delete, a cascade from a parent deleted outside the admin, and raw writes record nothing. A later service that changes these rows or deletes their parents (#116's account deletion, a self-service API) calls the functions with its actor.

## Consequences

- Every admin path that changes these rows records an event, and the actor is always the Account that acted.
- The ORM, the shell, and code that skips `apps.audit.access` change these rows with no event. Code review keeps new writers on the functions.
- Any staff Account that changes one of these rows becomes an actor, so it cannot be hard-deleted (`AuditEvent.actor_user` uses `PROTECT`). #116 plans for this.
- A new admin that saves these rows, or deletes a row that cascades to them, adds `AccessAuditAdminMixin`.

## Alternatives considered

- Record from the model (a `save()` override and a `pre_delete` receiver), with the actor taken from the request through a middleware and a context variable. It would cover every ORM path and every cascade. Rejected by the project owner: the actor would come from global request state instead of being passed, unlike every other Audit Event, and a token-authenticated request would carry the wrong actor unless each caller set it.
- Signals only (`post_save`). Rejected: in autocommit mode `post_save` runs after the save's transaction, so the event and the change would not commit together.
