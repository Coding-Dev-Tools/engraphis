"""Structured logging setup — stdlib only, no new dependencies.

Plain human-readable lines by default (the format the server has always used);
one-line JSON per record when ``ENGRAPHIS_LOG_FORMAT=json`` (``ENGRAPHIS_LOG_JSON=1``
is kept as an alias) so a log shipper (Loki/CloudWatch/jq) can parse without
regexes. Level via ``ENGRAPHIS_LOG_LEVEL`` (default INFO). Any ``extra={...}``
fields a caller attaches (e.g. the request-id middleware's
``request_id``/``duration_ms``) are included in the JSON output.
"""
from __future__ import annotations

import json
import logging
import os
import time

_PLAIN_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"

# Attributes present on every LogRecord — everything else came in via ``extra=``.
_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message, plus any extras."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + ".%03dZ" % (record.msecs,),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level_env: str = "ENGRAPHIS_LOG_LEVEL",
                      format_env: str = "ENGRAPHIS_LOG_FORMAT",
                      json_env: str = "ENGRAPHIS_LOG_JSON") -> None:
    """Configure root logging from env. Idempotent — safe to call more than once
    (replaces the root handler instead of stacking duplicates)."""
    level_name = os.environ.get(level_env, "INFO").strip().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    fmt = os.environ.get(format_env, "").strip().lower()
    if fmt in ("json", "text"):
        use_json = fmt == "json"
    else:  # unset/unknown → legacy boolean alias, default text
        use_json = os.environ.get(json_env, "").strip().lower() in ("1", "true", "yes", "on")
    handler = logging.StreamHandler()
    if use_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
