"""Structured logging configuration (see §6.6 of the design brief).

Key invariant: SMILES bodies and MolBlocks must never appear in log records.
Logging callers pass identifiers and sizes only (mol_id, created_by,
smiles_len, num_atoms, etc.) via the ``extra=`` parameter; the JSON
formatter surfaces those structured fields and the human-readable
``message`` while dropping everything else.
"""
from __future__ import annotations

import json
import logging
import logging.config
import sys

# Structured fields allowed in the JSON payload. Anything not in this set
# is dropped from the formatter output, which makes it harder to leak a
# SMILES body by passing it through ``extra=``.
_ALLOWED_EXTRA_FIELDS: frozenset[str] = frozenset(
    {
        "mol_id",
        "created_by",
        "channel_id",
        "team_id",
        "num_atoms",
        "smiles_len",
        "molblock_len",
        "input_name_len",
        "elapsed_ms",
        "error_kind",
        "backend",
        "attempt",
        "status_code",
        "path",
        "method",
    }
)


class JsonFormatter(logging.Formatter):
    """Single-line JSON formatter for Cloud Run / gcloud log ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _ALLOWED_EXTRA_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class AccessLogQueryStringFilter(logging.Filter):
    """Strip the ``?...`` query-string portion from uvicorn access logs.

    Uvicorn's default access log includes the request line including the
    query string; for this service the query string can carry user input
    we do not want to persist in logs. Replace any '?...' segment with
    '?<masked>' in the rendered message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "?" in msg:
            # Conservative replacement: keep everything up to '?' then
            # mask the rest of that token.
            head, _, tail = msg.partition("?")
            # Tail may include trailing 'HTTP/1.1" 200' segments; only mask
            # up to the next whitespace.
            tail_head, sep, tail_rest = tail.partition(" ")
            record.msg = f"{head}?<masked>{sep}{tail_rest}"
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {"()": JsonFormatter},
            },
            "filters": {
                "access_qs": {"()": AccessLogQueryStringFilter},
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "json",
                },
                "stdout_access": {
                    "class": "logging.StreamHandler",
                    "stream": sys.stdout,
                    "formatter": "json",
                    "filters": ["access_qs"],
                },
            },
            "root": {"level": level, "handlers": ["stdout"]},
            "loggers": {
                "uvicorn": {
                    "level": level,
                    "handlers": ["stdout"],
                    "propagate": False,
                },
                "uvicorn.error": {
                    "level": level,
                    "handlers": ["stdout"],
                    "propagate": False,
                },
                "uvicorn.access": {
                    "level": level,
                    "handlers": ["stdout_access"],
                    "propagate": False,
                },
            },
        }
    )
