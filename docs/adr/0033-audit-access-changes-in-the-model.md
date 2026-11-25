# 0033. Audit access changes in the model, with the request's Account as actor

- Status: Proposed
- Date: 2026-11-24

## Context

Organizer Members, Club Roles, Club Organizer Grants, and Organizer Application Grants decide who may submit counted matches (ADR 0021, CLAUDE.md hard rule 8). Until #158, only Django's admin log recorded their changes, and #116 clears it. Other Audit Events are written through `record_event()` (ADR 0007) by services that take an explicit `actor`. These rows have no service: the admin saves them through inlines and its own add, change, and delete views, and they also go when a Club, Organizer, Partner Application, or Account is deleted, by cascade. A service-only rule would miss the cascades and every admin path that skipped it.

## Decision

- **The model records.** The four models inherit `apps.audit.access.AuditedAccessModel`. Its `save()` reads the stored row under a lock, saves, reads it again, and records an add or a change in the same atomic block. Nothing is recorded when the values are unchanged or the save raises. A `pre_delete` receiver, connected for each concrete subclass, records a removal from the stored row before the delete. Django sends it inside the delete's transaction for `delete()`, `QuerySet.delete()`, and cascades. Raw writes skip it, as they skip the other model rules.
- **The actor comes from the request.** `AuditActorMiddleware`, after `AuthenticationMiddleware`, holds `request.user` in a context variable for the request (`apps.audit.actor`). The hooks read it with `current_actor()`. Outside a request there is no actor unless code sets one with `acting_as()`. A token-authenticated API request has an anonymous `request.user`, so a service it calls sets `acting_as(request.auth)`.
- **An Account deleting itself is not the actor of its own removals.** An event naming it would point at a row gone at commit and fail the whole delete. The event has no actor, and its `before` holds the Account's row id. The check reads the delete's `origin`, so Account deletion (#116) deletes through `delete()` or `QuerySet.delete()`, not a bare `Collector`.
- **Values.** `before` and `after` hold the role, OPEN Codes, the Account's row id, and the application's `client_id`. Never an email or an Organizer's name (ADR 0009).

## Consequences

- Every save and delete of these rows records an event, including paths added later, with no change to the admin. Raw writes (`bulk_create`, `QuerySet.update`, `bulk_update`, raw SQL, `loaddata`) still skip it.
- Any staff Account that changes one of these rows becomes an actor, so it cannot be hard-deleted (`AuditEvent.actor_user` uses `PROTECT`). #116 plans for this.
- A later model that grants submission authority or consent inherits `AuditedAccessModel` and adds its own `AuditAction` values.
- A change made in a request is attributed to whoever `request.user` resolved to. A request that logs in or out and then changes one of these rows would record that user. No such view exists.

## Alternatives considered

- Services with an explicit actor, called from admin overrides (`save_formset`, `delete_model`, `delete_queryset`) on every admin that can remove these rows, the Account admin included. Rejected: many entry points to keep in step, and a cascade from any other path would go unrecorded.
- Signals only (`post_save`). Rejected: in autocommit mode `post_save` runs after the save's transaction, so the event and the change would not commit together.
