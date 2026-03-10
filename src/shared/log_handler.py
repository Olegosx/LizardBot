"""Logging handler для записи логов в SharedState (отображение на фронтенде)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.shared.models import LogEntry

if TYPE_CHECKING:
    from src.shared.state import SharedState


class SharedLogHandler(logging.Handler):
    """Перехватывает все log-записи и дублирует их в SharedState.log_buffer.

    Подключается в main.py к корневому логгеру. Благодаря этому любое
    logging.info/warning/error автоматически появляется на фронтенде.
    """

    def __init__(self, shared: SharedState) -> None:
        super().__init__()
        self._shared = shared

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogEntry(
                timestamp=datetime.now(timezone.utc),
                level=record.levelname,
                thread=record.threadName,
                message=self.format(record),
            )
            self._shared.add_log(entry)
        except Exception:
            self.handleError(record)
