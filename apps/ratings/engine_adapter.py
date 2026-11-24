"""The backend's one link to the rating engine (``open_engine``).

This is the only module that imports ``open_engine`` (#44). The backend
never computes a rating itself (hard rule 2): it calls the engine through
this module. The engine is pinned to a git tag in ``pyproject.toml`` and
``uv.lock`` (see ``docs/engine.md``).
"""

import open_engine


def engine_version() -> str:
    """Return the version of the installed engine."""
    return open_engine.__version__
