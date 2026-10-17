"""Structured logging for every agent entry point (AC-AGT-8).

``configure_logging`` installs one handler on the ``nasa_agent`` logger that writes one JSON object per line: ts, level,
logger, message and every structured ``extra`` field (request_id, tool, ok, error_kind, latency_ms, steps, stop, ...).
Level comes from ``LOG_LEVEL`` (default INFO). Call sites only ever pass ids, names, status and latency as extras -
never message content, tool payloads or images - so the formatter writes extras verbatim.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import IO

HANDLER_NAME = "nasa_agent.json"
_STANDARD = set(vars(logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_") and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def _level(level: str | int | None) -> int:
    raw = level if level is not None else os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(raw, int):
        return raw
    value = logging.getLevelName(str(raw).strip().upper())
    return value if isinstance(value, int) else logging.INFO


def configure_logging(level: str | int | None = None, *, stream: IO[str] | None = None) -> logging.Logger:
    """Install (or replace) the JSON handler on the ``nasa_agent`` logger. Idempotent."""
    logger = logging.getLogger("nasa_agent")
    for handler in [h for h in logger.handlers if h.get_name() == HANDLER_NAME]:
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(_level(level))
    return logger
