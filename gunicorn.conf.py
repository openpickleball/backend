"""gunicorn settings, read from the repo root by `uv run gunicorn`.

PORT (default 8000) and WEB_CONCURRENCY (default 2, the number of worker
processes) come from the process environment only, never from .env.
DigitalOcean App Platform sets PORT. A bad PORT stops startup with an
error that names the variable and never echoes the value; a blank PORT
means the default.

gunicorn itself parses WEB_CONCURRENCY with int() before it reads this
file, so a blank or non-integer value stops startup with gunicorn's own
error. This file refuses the integers gunicorn accepts that are not a
plain number of at least 1. Run `manage.py collectstatic --noinput`
first, so WhiteNoise has the static files to serve.
"""

import os


def _whole_number(
    name: str, default: int, low: int, high: int | None, *, blank_is_unset: bool
) -> int:
    """Read a whole number from the environment, failing without echoing it."""
    raw = os.environ.get(name, "").strip()
    if name not in os.environ or (blank_is_unset and not raw):
        return default
    # isdigit() alone accepts non-ASCII digits, which int() would read.
    if raw.isascii() and raw.isdigit():
        value = int(raw)
        if value >= low and (high is None or value <= high):
            return value
    limit = f"from {low} to {high}" if high is not None else f"of at least {low}"
    raise ValueError(f"Set {name} to a whole number {limit}, or leave it unset.")


wsgi_app = "config.wsgi:application"
bind = f"0.0.0.0:{_whole_number('PORT', 8000, 1, 65535, blank_is_unset=True)}"
workers = _whole_number("WEB_CONCURRENCY", 2, 1, None, blank_is_unset=False)
# gunicorn 27 opens a Unix control socket by default that accepts reload,
# shutdown, and worker commands. Nothing uses it, so it stays off.
control_socket_disable = True
