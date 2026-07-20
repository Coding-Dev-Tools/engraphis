import json
import logging

from engraphis.observability import (
    RedactedJsonFormatter,
    configure_structured_logging,
)


def test_structured_logs_redact_credentials_and_customer_identifiers():
    record = logging.LogRecord(
        "engraphis.test", logging.ERROR, __file__, 1,
        "delivery to buyer@example.com failed: Bearer abcdefghijklmnop "
        "ENGRAPHIS_API_KEY=topsecret", (), None)
    payload = json.loads(RedactedJsonFormatter().format(record))
    assert payload["level"] == "error"
    assert payload["logger"] == "engraphis.test"
    assert "buyer@example.com" not in payload["event"]
    assert "abcdefghijklmnop" not in payload["event"]
    assert "topsecret" not in payload["event"]
    assert "[email]" in payload["event"]


def test_structured_logs_redact_mapping_url_and_dsn_secret_forms():
    record = logging.LogRecord(
        "engraphis.test", logging.DEBUG, __file__, 1,
        'POST https://provider.test/generate?key=google-secret&model=safe '
        'payload={"api_key": "json-secret", \'password\': \'python-secret\'} '
        'dsn=postgresql://dbuser:db-secret@database.test/app', (), None)
    event = json.loads(RedactedJsonFormatter().format(record))["event"]
    assert "google-secret" not in event
    assert "json-secret" not in event
    assert "python-secret" not in event
    assert "dbuser" not in event
    assert "db-secret" not in event
    assert "model=safe" in event
    assert event.count("[redacted]") >= 4


def test_configure_structured_logging_is_idempotent(monkeypatch):
    loggers: dict[str, logging.Logger] = {}

    def isolated_logger(name=None):
        key = name or ""
        return loggers.setdefault(key, logging.Logger(key))

    monkeypatch.setattr(logging, "getLogger", isolated_logger)
    monkeypatch.setenv("ENGRAPHIS_JSON_LOGS", "1")

    assert configure_structured_logging() is True
    root = isolated_logger()
    assert len(root.handlers) == 1
    original_handler = root.handlers[0]
    assert isinstance(original_handler.formatter, RedactedJsonFormatter)

    assert configure_structured_logging() is True
    assert root.handlers == [original_handler]
    assert isinstance(original_handler.formatter, RedactedJsonFormatter)
