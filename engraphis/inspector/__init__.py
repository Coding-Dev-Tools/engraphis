"""Engraphis Inspector -- internal single-user compatibility API.

The standalone Inspector product (:8710) was retired 2026-07-10; its memory-inspection
features were merged into the unified dashboard on :8700. The remaining
``engraphis.inspector.app`` module is a thin, optionally bearer-protected JSON binding
for local inspection. It contains no Team identity, seat, subscription, analytics, or
automation authority; those are hosted cloud services.

This is a library surface, not an entrypoint. Use
``python -m scripts.start_dashboard`` for the local product UI.
"""


def create_app(*args, **kwargs):
    """Load the legacy FastAPI binding only when a caller actually starts it."""
    from engraphis.inspector.app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
