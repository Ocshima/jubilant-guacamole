"""
Structured JSON logger for the trade ETL processor.

All log output is structured JSON so CloudWatch Metric Filters can parse
specific fields to produce custom metrics without any custom metrics API
calls inside the Lambda code itself.

Log schema (all records include these fields):
  - timestamp:    ISO 8601 UTC
  - level:        INFO | WARNING | ERROR
  - event:        snake_case event name (used by metric filters)
  - request_id:   Lambda request ID (injected at handler entry)
  - environment:  dev | prod (from ENV var)
  - ...plus event-specific fields

Usage:
    from utils.logger import get_logger
    log = get_logger()
    log.info("record_processed", trade_id="T123", symbol="AAPL", duration_ms=12)
    log.error("record_invalid", trade_id="T456", reason="negative price", price=-5.0)
"""

import json
import logging
import os
from datetime import datetime, timezone


class StructuredLogger:
    """
    Wraps Python's standard logger and emits structured JSON lines.
    Each call produces exactly one JSON object per line — compatible
    with CloudWatch Metric Filters which operate on single log events.
    """

    def __init__(self, name: str):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.DEBUG)
        self._request_id = "unknown"
        self._environment = os.environ.get("ENVIRONMENT", "dev")

        # Avoid duplicate handlers if the module is re-imported in warm Lambda
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def set_request_id(self, request_id: str) -> None:
        """Call at the top of each Lambda invocation with context.aws_request_id."""
        self._request_id = request_id

    def _emit(self, level: str, event: str, **kwargs) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event": event,
            "request_id": self._request_id,
            "environment": self._environment,
            **kwargs,
        }
        msg = json.dumps(record, default=str)
        getattr(self._logger, level.lower())(msg)

    def info(self, event: str, **kwargs) -> None:
        self._emit("INFO", event, **kwargs)

    def warning(self, event: str, **kwargs) -> None:
        self._emit("WARNING", event, **kwargs)

    def error(self, event: str, **kwargs) -> None:
        self._emit("ERROR", event, **kwargs)


# Module-level singleton — one logger per Lambda container
_logger_instance: StructuredLogger | None = None


def get_logger() -> StructuredLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = StructuredLogger("trade-processor")
    return _logger_instance
