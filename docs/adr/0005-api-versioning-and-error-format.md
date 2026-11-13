# 0005. API versioning and error format

- Status: Proposed
- Date: 2026-11-12

## Context

Partners, apps, and the OPEN frontend will call the backend's API. The frontend generates its TypeScript client from the OpenAPI document (issue #5), so the URL prefix, the method names in the client, and the shape of error responses are part of the contract from the first endpoint. Changing them later breaks every generated client.

## Decision

- There is one django-ninja API, mounted at `/api/v1/`. Its OpenAPI document is served at `/api/v1/openapi.json`, with interactive docs at `/api/v1/docs`.
- Every operation has an explicit `operation_id`, which becomes the method name in generated clients. Building the OpenAPI document raises an error for an operation in the document that has none, and a test checks every operation.
- These errors come back in one shape, `ErrorResponse`: `{"code", "message", "errors": [{"field", "code", "message"}]}`. This covers validation errors, a body that does not parse, `Http404`, and `HttpError` (including authentication, permission, and throttling errors). `errors` is empty except for validation errors. With DEBUG off, any other exception goes to Django, which returns its own HTML error page (a 500, or another status for exceptions Django maps, such as 403 for `PermissionDenied`). Code raises `HttpError(403, ...)` rather than `PermissionDenied`.
- Validation errors return 422 with one item per pydantic error. `field` is the dotted location of the input, such as `path.pid`, `query.page`, or `body.items.0.k`. A plain error that django-ninja raises outside pydantic, such as its cursor pagination error, names only the parameter (`cursor`). Every operation that takes input documents this 422 response. An operation that takes input may not declare a different 422 schema: building the document raises an error.
- The response never copies pydantic's `input` or `ctx` details. Validation messages must not contain the submitted value.

## Consequences

- Generated clients get stable method names and one error type to handle.
- The readiness check (`GET /api/v1/health/ready`, issue #6) is the one operation that returns a non-2xx body of its own. Its 503 is a status report, not an error: the same `ReadyOut` schema as its 200, with a status and a per-check status, documented in the OpenAPI document. Probes read it by status code.
- The per-item error shape is the one the match submission endpoints need (issue #80), so single and bulk endpoints can share it.
- Some errors come from outside the operation and do not use `ErrorResponse`: django-ninja's 405 for a wrong method (plain text), and Django's 404 page for a path that matches no route. Clients should treat a non-JSON error body as a generic failure.
- The docs page loads Swagger UI from the app's own static files, not a CDN. With DEBUG off, WhiteNoise serves them once `collectstatic` has run (issue #10).

## Alternatives considered

- django-ninja's default errors (`{"detail": ...}`): `detail` is a list for validation errors and a string otherwise, so clients need two error types, and the field location includes the Python name of the body parameter.
- Generated `operation_id`s: django-ninja builds one from the module and function name, so renaming a view would rename the client method without anyone noticing.
- Versioning by header instead of URL: harder to try in a browser or with curl, and a URL prefix is a plain Django `path()`.
