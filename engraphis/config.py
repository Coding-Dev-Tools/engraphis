"""Central configuration — all values sourced from env with safe defaults."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Vendor-hosted managed service for sync, leases, trials, and invite delivery. This is
#: the default target when ``ENGRAPHIS_RELAY_URL`` is not overridden.
DEFAULT_RELAY_URL = "https://team.engraphis.com"

# Keys issued before the custom domain migration carry this URL inside their signed
# payload. Preserve the signature, but route that one retired vendor host to the current
# managed service. Arbitrary signed URLs remain authoritative.
RETIRED_RELAY_URLS = frozenset({
    "https://engraphis-production.up.railway.app",
})


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
        default_factory=lambda: _parse_origins(_env("ENGRAPHIS_CORS_ORIGINS", ""),
                                               _env_int("ENGRAPHIS_PORT", 8700))
    )
    # Optional server-side workspace binding — the hard multi-tenant isolation boundary.
    # When non-empty, MemoryService refuses any read or write whose
    # workspace is not in this comma-separated allow-list, so knowing or guessing a
    # workspace name is not enough to reach it. Empty = unrestricted (single-tenant local).
    allowed_workspaces: list = field(
        default_factory=lambda: _parse_csv(_env("ENGRAPHIS_WORKSPACES", ""))
    )
    # Team auth is ON by default (opt-out); set ENGRAPHIS_TEAM_MODE=0/false/no/off to
    # disable it. A Team license gates paid capabilities and additional seats, while an
    # existing user store keeps its login wall even if entitlement later lapses.
    team_mode: bool = field(
        default_factory=lambda: _env("ENGRAPHIS_TEAM_MODE", "").lower()
        not in ("0", "false", "no", "off")
    )

    # Managed relay base URL. Client sync uses it when `--relay-url` is omitted, and paid
    # license flows fall back to it when a signed key or explicit cloud override supplies
    # no URL. Set an empty ENGRAPHIS_RELAY_URL to require an explicit target.
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
    embed_dim: Optional[int] = field(
        default_factory=lambda: (
            _env_int("ENGRAPHIS_EMBED_DIM", 384) or None
        )
    )

    # Fact extraction on the v2 write path: "none" (default — store text as given),
    # "chunk" (deterministic, offline structure-aware chunking — knobs
    # ENGRAPHIS_CHUNK_TOKENS/_OVERLAP/_MAX), or "llm" (distill raw text into discrete
    # facts via the configured LLM before storing).
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

    # Optional host-LLM importance/retention classification. "none" keeps the fully
    # deterministic local write path; "llm" asks the configured provider for a bounded
    # ephemeral/normal/critical signal and degrades safely on any failure.
    retention_supervisor: str = field(
        default_factory=lambda: _env("ENGRAPHIS_RETENTION_SUPERVISOR", "none").lower()
    )

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
        """Connectable local base URL (wildcard binds map to loopback, IPv6 literals are
        bracketed — ``host='::'`` must not yield the malformed ``http://:::8700``)."""
        from engraphis.netutil import display_base_url
        return display_base_url(self.host, self.port)


def _parse_headers(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _parse_origins(raw: str, port: int = 8700) -> list:
    """CORS allow-list. Empty -> loopback on the CONFIGURED port (safe local-first default).

    Deriving the default from ``port`` means running the dashboard on a non-default
    ENGRAPHIS_PORT doesn't lock its own origin out of the CORS allow-list."""
    if not raw.strip():
        return ["http://127.0.0.1:%d" % port, "http://localhost:%d" % port]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _parse_csv(raw: str) -> list:
    """Generic comma-separated allow-list. Empty -> [] (no restriction)."""
    return [item.strip() for item in raw.split(",") if item.strip()]


settings = Settings()


def canonicalize_relay_url(url: str) -> str:
    """Normalize a relay URL and migrate known retired vendor hosts."""
    normalized = (url or "").strip().rstrip("/")
    return DEFAULT_RELAY_URL if normalized in RETIRED_RELAY_URLS else normalized


def resolve_license_server_url(signed_url: str = "") -> str:
    """Resolve the license server, including known vendor-host migrations."""
    override = canonicalize_relay_url(_env("ENGRAPHIS_CLOUD_URL", ""))
    signed = canonicalize_relay_url(signed_url)
    relay = canonicalize_relay_url(settings.relay_url)
    return override or signed or relay
