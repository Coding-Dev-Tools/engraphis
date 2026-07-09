"""configure_logging — env-driven level, plain format by default, one-line JSON mode.

Stdlib-only, so these run on the numpy-only CI floor too.
"""
import json
import logging

import pytest

from engraphis.logging_setup import JsonFormatter, configure_logging


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_plain_human_format_by_default(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_LOG_JSON", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LOG_FORMAT", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LOG_LEVEL", raising=False)
    configure_logging()
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)


def test_level_env_is_respected_and_bad_values_fall_back(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LOG_LEVEL", "warning")
    configure_logging()
    assert logging.getLogger().level == logging.WARNING
    monkeypatch.setenv("ENGRAPHIS_LOG_LEVEL", "not-a-level")
    configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_reconfigure_is_idempotent_no_handler_stacking(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_LOG_JSON", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LOG_FORMAT", raising=False)
    configure_logging()
    configure_logging()
    assert len(logging.getLogger().handlers) == 1


def test_json_mode_emits_one_line_json_with_extras(monkeypatch, capsys):
    monkeypatch.setenv("ENGRAPHIS_LOG_JSON", "1")
    monkeypatch.delenv("ENGRAPHIS_LOG_FORMAT", raising=False)
    monkeypatch.delenv("ENGRAPHIS_LOG_LEVEL", raising=False)
    configure_logging()
    logging.getLogger("engraphis.test").info(
        "GET /x -> %d", 200, extra={"request_id": "abc123", "duration_ms": 1.2})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)                       # valid single-line JSON
    assert payload["message"] == "GET /x -> 200"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "engraphis.test"
    assert payload["request_id"] == "abc123"         # extras survive into the record
    assert payload["duration_ms"] == 1.2


def test_log_format_env_selects_json_and_overrides_alias(monkeypatch, capsys):
    monkeypatch.setenv("ENGRAPHIS_LOG_FORMAT", "json")
    monkeypatch.delenv("ENGRAPHIS_LOG_JSON", raising=False)
    configure_logging()
    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
    # An explicit format wins over the legacy boolean alias.
    monkeypatch.setenv("ENGRAPHIS_LOG_FORMAT", "text")
    monkeypatch.setenv("ENGRAPHIS_LOG_JSON", "1")
    configure_logging()
    assert not isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)
