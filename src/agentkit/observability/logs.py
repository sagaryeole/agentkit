"""Structured logs: one JSON object per line, redacted, carrying the run ID.

Run code logs through `ctx.logger`, which stamps each record with the run ID, so a log
line can be matched to the trace of the run that wrote it (`agentkit trace <run_id>`).
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

from agentkit.config import Config
from agentkit.config.secrets import known_secret_values
from agentkit.guardrails.checks import redact

LOGGER_NAME = "agentkit"


class JsonFormatter(logging.Formatter):
    """Formats a record as one JSON line, with secrets and personal data removed."""

    def __init__(self, patterns: list[str], secrets: list[str]) -> None:
        super().__init__()
        self._patterns, self._secrets = patterns, secrets

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        for key in ("message", "exception"):
            if key in entry:
                entry[key] = redact(str(entry[key]), self._patterns, self._secrets)
        return json.dumps(entry)


def configure_logging(cfg: Config, stream: TextIO | None = None) -> None:
    """Send the kit's logs to `stream` (default stderr) at `observability.log_level`.

    Call it once at start-up; the CLI does. Calling it again replaces the earlier setup.
    The kit's logger does not pass records on to the root logger, so an application that
    prefers its own logging setup should not call this and can attach handlers to the
    "agentkit" logger instead.
    """
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, "_agentkit", False):
            logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler._agentkit = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter(cfg.guardrails.redact_patterns, known_secret_values()))
    logger.addHandler(handler)
    logger.setLevel(cfg.observability.log_level)
    logger.propagate = False
