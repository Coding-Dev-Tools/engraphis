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

    db_path: str = field(
        default_factory=lambda: _env(
            "ENGRAPHIS_DB_PATH",
            str(_PROJECT_ROOT / "neocortex.db"),
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

    llm_provider: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_PROVIDER", "openai").lower())
    llm_model: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_MODEL", "gpt-4o-mini"))
    llm_api_key: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_BASE_URL", ""))
    llm_extra_headers: dict = field(
        default_factory=lambda: _parse_headers(_env("ENGRAPHIS_LLM_EXTRA_HEADERS", ""))
    )

    loop_interval: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_INTERVAL", 60))
    loop_top_k: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_TOP_K", 20))
    decay_halflife_days: float = field(
        default_factory=lambda: _env_float("ENGRAPHIS_DECAY_HALFLIFE_DAYS", 7.0)
    )

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


settings = Settings()
