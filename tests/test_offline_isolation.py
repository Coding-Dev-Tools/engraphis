"""The documented offline suite must stay inert even on an owner's machine."""
from __future__ import annotations

import os

from engraphis.config import settings

# Keep in sync with tests/conftest.py::OFFLINE_PROVIDER_ENV_VARS.
PROVIDER_ENV_VARS = (
    "ENGRAPHIS_EXTRACTOR",
    "ENGRAPHIS_LLM_AUTO_EXTRACT",
    "ENGRAPHIS_RETENTION_SUPERVISOR",
    "ENGRAPHIS_DECISION_BACKEND",
    "ENGRAPHIS_LLM_PROVIDER",
    "ENGRAPHIS_LLM_MODEL",
    "ENGRAPHIS_LLM_API_KEY",
    "ENGRAPHIS_LLM_BASE_URL",
    "TYPESAFE_API_KEY",
    "TYPESAFE_BASE_URL",
    "JEV_API_KEY",
    "ENGRAPHIS_DECISION_MODEL",
)


def test_offline_suite_pins_provider_defaults() -> None:
    assert settings.extractor == "none"
    assert settings.llm_auto_extract is False
    assert settings.retention_supervisor == "none"
    assert settings.decision_backend == "none"
    assert settings.llm_provider == "openai"
    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_api_key == ""
    assert settings.llm_base_url == ""
    assert settings.typesafe_api_key == ""
    assert settings.decision_model == "jev-1.13.0"
    for name in PROVIDER_ENV_VARS:
        assert name not in os.environ
