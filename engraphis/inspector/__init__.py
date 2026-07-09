"""Engraphis Memory Inspector — the v2 product UI.

Served from the same ``MemoryService`` layer that backs the MCP server, so the UI and
the AI-facing tools can never drift: every panel is a thin view over the exact API the
agent uses. Run with ``python -m scripts.inspector`` (default http://127.0.0.1:8710).
"""
from engraphis.inspector.app import create_app

__all__ = ["create_app"]
