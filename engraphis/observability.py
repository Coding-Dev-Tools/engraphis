"""Redacted JSON logging for hosted customer and control-plane services."""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_LICENSE = re.compile(r"\bENGR1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}")
_ASSIGNMENT = re.compile(
    # URL query/fragment assignments are handled parameter-by-parameter by
    # _URL_SECRET. Do not let this environment-assignment fallback consume the rest of
    # a URL after the first redacted value (and thereby erase later parameter names).
    r"(?i)(?<![?&#])\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|SIGNING_KEY))="
    r"([^\s,;&]+)")
_SENSITIVE_NAME = (
    r"(?:[A-Z0-9_.-]*(?:TOKEN|SECRET|PASSWORD|CREDENTIAL)|"
    r"[A-Z0-9_.-]*(?:API|SIGNING|LICENSE|PRIVATE)[_-]?KEY|"
    r"KEY|AUTHORIZATION|COOKIE|SIGNATURE|SIG|CODE)"
)
_COLON_SECRET = re.compile(
    rf"(?i)(?P<prefix>(?<![A-Z0-9_.-])[\"']?{_SENSITIVE_NAME}[\"']?\s*:\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)"
)
_URL_SECRET = re.compile(
    rf"(?i)(?P<prefix>[?&#;]{_SENSITIVE_NAME}=)[^&#\s]*"
)
_URL_USERINFO = re.compile(
    r"(?i)([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"
)


def redact(value: object) -> str:
    """Remove common credential and customer-identifier shapes from one log field."""
    text = str(value)
    text = _LICENSE.sub("[license]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    # Provider URLs can carry credentials in query parameters (Google's LLM API uses
    # ``?key=...``), while ASGI access logs may contain invite/reset codes. Preserve the
    # parameter name and all non-sensitive parameters so the request remains diagnosable.
    text = _URL_SECRET.sub(lambda match: match.group("prefix") + "[redacted]", text)
    text = _URL_USERINFO.sub(r"\1[redacted]:[redacted]@", text)
    # Exceptions and provider SDKs commonly render dictionaries as either JSON or Python
    # reprs. Assignment-only redaction did not cover ``{\"api_key\": \"...\"}`` or
    # ``token: value`` forms, so a structured formatter could still serialize a secret.
    text = _COLON_SECRET.sub(
        lambda match: match.group("prefix") + '"[redacted]"', text)
    text = _ASSIGNMENT.sub(r"\1=[redacted]", text)
    return _EMAIL.sub("[email]", text)


class RedactedJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name[:120],
            "event": redact(record.getMessage())[:4000],
        }
        if record.exc_info and record.exc_info[0]:
            payload["error_type"] = record.exc_info[0].__name__[:120]
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


def configure_structured_logging() -> bool:
    """Install the redacted formatter when ``ENGRAPHIS_JSON_LOGS`` is enabled."""
    if os.environ.get("ENGRAPHIS_JSON_LOGS", "").strip().lower() not in (
            "1", "true", "yes", "on"):
        return False
    formatter = RedactedJsonFormatter()
    configured = False
    for name in ("", "engraphis", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            handler.setFormatter(formatter)
            configured = True
    if not configured:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)
    return True
