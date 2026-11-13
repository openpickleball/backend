"""gunicorn settings, read from the repo root by `uv run gunicorn`.

PORT (default 8000) and WEB_CONCURRENCY (default 2, the number of worker
processes) come from the environment. DigitalOcean App Platform sets
PORT. A bad value stops startup with an error that names the variable
and never echoes the value. Run `manage.py collectstatic --noinput`
first, so WhiteNoise has the static files to serve.
"""

import os


def _whole_number(name: str, default: int, low: int, high: int | None) -> int:
    """Read a whole number from the environment; blank means the default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    # isdigit() alone accepts non-ASCII digits, which int() would read.
    if raw.isascii() and raw.isdigit():
        value = int(raw)
        if value >= low and (high is None or value <= high):
            return value
    limit = f"from {low} to {high}" if high is not None else f"of at least {low}"
    raise ValueError(f"Set {name} to a whole number {limit}, or leave it unset.")


wsgi_app = "config.wsgi:application"
bind = f"0.0.0.0:{_whole_number('PORT', 8000, 1, 65535)}"
workers = _whole_number("WEB_CONCURRENCY", 2, 1, None)
