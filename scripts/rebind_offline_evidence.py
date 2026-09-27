"""Compatibility entry point for fresh, immutable offline measurements.

Historical evidence is never rebound. Pass --output with a new artifact path;
the canonical exporter reruns the fixtures and binds the exact evaluated source.
"""
from __future__ import annotations

if __package__:
    from .export_offline_evidence import main
else:
    from export_offline_evidence import main


if __name__ == "__main__":
    raise SystemExit(main())
