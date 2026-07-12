"""Central configuration — all values sourced from env with safe defaults."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Vendor-hosted managed sync relay — the default target when ``ENGRAPHIS_RELAY_URL``
#: isn't overridden. Single source of truth: the dashboard's one-click sync
#: (``routes/v2_api.py``) imports this rather than re-declaring the literal.
DEFAULT_RELAY_URL = "https://engraphis-production.up.railway.app"


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("ENGRAPHIS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("ENGRAPHIS_PORT", 8700))

    # Optional bearer token. When non-empty, the REST API requires
    # `Authorization: Bearer <token>` on all routes except health/docs/dashboard.
    api_token: str = field(default_factory=lambda: _env("ENGRAPHIS_API_TOKEN", ""))
    # Comma-separated CORS allow-list. Defaults to loopback only (local-first).
    cors_origins: list = field(
        default_factory=lambda: _parse_origins(_env("ENGRAPHIS_CORS_ORIGINS", ""))
    )
    # Optional server-side workspace binding — the hard multi-tenant isolation boundary.
    # When non-empty, MemoryService refuses any read or write whose
    # workspace is not in this comma-separated allow-list, so knowing or guessing a
    # workspace name is not enough to reach it. Empty = unrestricted (single-tenant local).
    allowed_workspaces: list = field(
        default_factory=lambda: _parse_csv(_env("ENGRAPHIS_WORKSPACES", ""))
    )
    # Team mode (Pro): multi-user Inspector logins/roles. Only takes effect with a
    # license carrying the 'team' feature; without one the Inspector reports
    # upgrade-required instead of enabling auth (engraphis/inspector/app.py).
    team_mode: bool = field(
        default_factory=lambda: _env("ENGRAPHIS_TEAM_MODE", "").lower()
        in ("1", "true", "yes", "on")
    )

    # Managed cloud-sync relay base URL (client side). When set, `python -m scripts.sync
    # --relay` (or `--relay-url` omitted) targets this host instead of a shared folder.
    # The relay is the headline Pro sync transport; the server half is mounted by
    # `inspector/cloud_mount.py`. Empty = no default relay (use --remote folder sync or
    # pass --relay-url explicitly). See docs/SYNC.md.
    relay_url: str = field(default_factory=lambda: _env(
        "ENGRAPHIS_RELAY_URL", DEFAULT_RELAY_URL))

    db_path: str = field(
        default_factory=lambda: _env(
            "ENGRAPHIS_DB_PATH",
            str(_PROJECT_ROOT / "engraphis.db"),
        )
    )

    embed_model: str = field(
        default_factory=lambda: _env(
            "ENGRAPHIS_EMBED_MODEL",
            "sentence-transformers/all-MiniLM-L6-v2",
        )
    )
    embed_dim: int | None = field(
        default_factory=lambda: (
            _env_int("ENGRAPHIS_EMBED_DIM", 0) or None
        )
    )

    # Fact extraction on the v2 write path: "none" (default — store text as given) or
    # "llm" (distill raw text into discrete facts via the configured LLM before storing).
    extractor: str = field(default_factory=lambda: _env("ENGRAPHIS_EXTRACTOR", "none").lower())

    llm_provider: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_PROVIDER", "openai").lower())
    llm_model: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_MODEL", "gpt-4o-mini"))
    llm_api_key: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_BASE_URL", ""))
    llm_extra_headers: dict = field(
        default_factory=lambda: _parse_headers(_env("ENGRAPHIS_LLM_EXTRA_HEADERS", ""))
    )

    # Optional cross-encoder reranker model. Empty (default) -> IdentityReranker (offline).
    rerank_model: str = field(default_factory=lambda: _env("ENGRAPHIS_RERANK_MODEL", ""))

    # Graph extractor for the knowledge-graph tab: "regex" (default) = dependency-free
    # heuristic NER, no API key, populated on every ingest; "none" disables graph
    # population. Defaults on so the Graph tab works out of the box for every install.
    graph_extractor: str = field(default_factory=lambda: _env("ENGRAPHIS_GRAPH_EXTRACTOR", "regex").lower())

    loop_interval: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_INTERVAL", 60))
    loop_top_k: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_TOP_K", 20))
    decay_halflife_days: float = field(
        default_factory=lambda: _env_float("ENGRAPHIS_DECAY_HALFLIFE_DAYS", 7.0)
    )

    # Optional in-process rate limiting for the v1 REST API (per-client-IP sliding window).
    # 0 = disabled (default), matching the loopback-first posture; set both to enable.
    rate_limit: int = field(default_factory=lambda: _env_int("ENGRAPHIS_RATE_LIMIT", 0))
    rate_window: int = field(default_factory=lambda: _env_int("ENGRAPHIS_RATE_WINDOW", 60))

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _parse_headers(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _parse_origins(raw: str) -> list:
    """CORS allow-list. Empty -> loopback only (safe local-first default)."""
    if not raw.strip():
        return ["http://127.0.0.1:8700", "http://localhost:8700"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _parse_csv(raw: str) -> list:
    """Generic comma-separated allow-list. Empty -> [] (no restriction)."""
    return [item.strip() for item in raw.split(",") if item.strip()]


settings = Settings()
