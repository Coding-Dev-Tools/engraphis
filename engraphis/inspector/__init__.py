"""Engraphis Inspector — INTERNAL / LEGACY API layer (no longer a shipped product).

The standalone Inspector product (:8710) was retired 2026-07-10; its memory-inspection
features were merged into the unified dashboard on :8700 (see engraphis/static/index.html
and engraphis/routes/v2_api.py). This package is kept because:

  * ``engraphis.inspector.auth``     — shared multi-user auth used by the dashboard/Team
  * ``engraphis.inspector.webhooks`` — Polar key issuance used by engraphis.billing
  * ``engraphis.inspector.app``      — a thin FastAPI binding still exercised by the tests

It is a library surface, not an entrypoint. Use ``python -m scripts.start_dashboard``.
"""
def create_app(*args, **kwargs):
    """Load the legacy FastAPI binding only when a caller actually starts it."""
    from engraphis.inspector.app import create_app as factory
    return factory(*args, **kwargs)

__all__ = ["create_app"]
