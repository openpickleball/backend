# syntax=docker/dockerfile:1

# The production image: gunicorn by default; the same image runs the
# Celery worker and beat when the command is overridden (see README.md).
#
# Base images are pinned by tag and digest. To refresh a pin, pull the
# tag (`docker pull python:3.13-slim-bookworm`), read its digest with
# `docker image inspect --format '{{index .RepoDigests 0}}' <tag>`, and
# update every line that uses it. Both Python stages must use the same
# digest, so the virtualenv's interpreter link and compiled extensions
# match. The Python minor must match .python-version (a test checks it).

FROM ghcr.io/astral-sh/uv:0.13.10@sha256:a3ea1357342c1a4bf9a0c53a3deb1b55379aefa037338d8113d5771efa01774f AS uv

FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3 AS build
# git: uv installs the engine from its git tag.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Runtime dependencies only, exactly as uv.lock pins them. The project
# has no build system, so uv installs no package for it.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=.python-version,target=.python-version \
    uv sync --locked --no-dev --no-install-project
COPY . .
# Throwaway placeholders, on this line only, so none persist in the
# image. DEBUG on needs no production values, and the installed apps and
# static storage do not depend on DEBUG. Nothing connects to them.
RUN DJANGO_DEBUG=true \
    DATABASE_URL=postgres://build@127.0.0.1:1/build \
    REDIS_URL=redis://127.0.0.1:1/0 \
    FRONTEND_URL=http://localhost:8081 \
    /app/.venv/bin/python manage.py collectstatic --noinput

FROM python:3.13-slim-bookworm@sha256:f0fa218d5b06f9b9235024fa061cde2cb141f726de52134471053d2d9b5324a3 AS runtime
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# The commit or tag the image is built from, as Sentry's release (#65).
# The image build passes it: --build-arg SENTRY_RELEASE=<commit>. Empty
# means events carry no release. Not a secret.
ARG SENTRY_RELEASE=""
ENV SENTRY_RELEASE=${SENTRY_RELEASE}
RUN groupadd --system --gid 10001 open \
    && useradd --system --uid 10001 --gid open --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin open
WORKDIR /app
# Owned by root, so the app user can read the code but not change it.
COPY --from=build /app /app
USER 10001:10001
EXPOSE 8000
# gunicorn reads gunicorn.conf.py from /app: PORT and WEB_CONCURRENCY
# come from the environment.
CMD ["gunicorn"]
