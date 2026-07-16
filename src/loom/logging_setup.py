"""Central logging configuration for Loom.

Every module that needs a logger should use ``get_logger(__name__)`` from
here instead of calling ``logging.getLogger`` directly, so log level,
format, and destination stay governed by a single configuration path
(``loom.yaml`` -> ``ServerConfig`` -> dashboard Settings page).
"""

from __future__ import annotations

import json
import logging
import logging.config
from typing import Any


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def build_dict_config(
    level: str = "info",
    fmt: str = "plain",
    destination: str = "stderr",
    file_path: str = "logs/loom.log",
) -> dict[str, Any]:
    """Build a ``logging.config.dictConfig``-compatible dict from Loom's config."""
    level = (level or "info").upper()
    formatter: dict[str, Any] = (
        {"()": "loom.logging_setup.JsonFormatter"}
        if fmt == "json"
        else {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S%z",
        }
    )
    handler: dict[str, Any] = (
        {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": file_path,
            "maxBytes": 10_000_000,
            "backupCount": 3,
            "formatter": "loom",
        }
        if destination == "file"
        else {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "loom",
        }
    )
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"loom": formatter},
        "handlers": {"loom": handler},
        "root": {"level": level, "handlers": ["loom"]},
        "loggers": {
            "uvicorn": {"level": level, "handlers": ["loom"], "propagate": False},
            "uvicorn.error": {"level": level, "handlers": ["loom"], "propagate": False},
            "uvicorn.access": {"level": level, "handlers": ["loom"], "propagate": False},
        },
    }


def configure_logging(
    level: str = "info",
    fmt: str = "plain",
    destination: str = "stderr",
    file_path: str = "logs/loom.log",
) -> None:
    """Apply logging configuration process-wide. Safe to call again to change settings live."""
    if destination == "file":
        import pathlib

        pathlib.Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig(build_dict_config(level, fmt, destination, file_path))
