"""WebSocket менеджер: управляет соединениями и пушит обновления из SharedState."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Set

from fastapi import WebSocket
from fastapi.encoders import jsonable_encoder

from src.shared.models import MarketState
from src.shared.state import SharedState

logger = logging.getLogger(__name__)

_PUSH_INTERVAL = 3   # секунд между периодическими пушами
_HISTORY_IN_MARKETS = 60  # последних точек prob_history в payload рынка


class WebSocketManager:
    """Хранит активные WS-соединения и периодически пушит данные из SharedState.

    push_loop() запускается как asyncio task при старте FastAPI.
    """

    def __init__(self, shared: SharedState) -> None:
        self._shared = shared
        self._connections: Set[WebSocket] = set()
        self._log_cursor: int = 0

    async def connect(self, ws: WebSocket) -> None:
        """Принимает соединение и отправляет начальный снимок состояния."""
        await ws.accept()
        self._connections.add(ws)
        logger.debug("WS подключён, всего: %d", len(self._connections))
        await self._push_snapshot(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        self._connections.discard(ws)
        logger.debug("WS отключён, всего: %d", len(self._connections))

    async def broadcast(self, event: str, data: Any) -> None:
        """Отправляет событие всем соединениям, удаляет мёртвые."""
        if not self._connections:
            return
        payload = {"event": event, "data": jsonable_encoder(data)}
        dead: Set[WebSocket] = set()
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(ws)
        self._connections -= dead

    async def push_loop(self) -> None:
        """Периодический цикл пушей (asyncio task)."""
        while True:
            await asyncio.sleep(_PUSH_INTERVAL)
            if not self._connections:
                continue
            try:
                await self._push_periodic()
            except Exception as exc:
                logger.error("Ошибка в push_loop: %s", exc)

    # ── Снимок при подключении ────────────────────────────────────────────

    async def _push_snapshot(self, ws: WebSocket) -> None:
        """Отправляет полное текущее состояние новому клиенту."""
        try:
            await ws.send_json({"event": "status",    "data": jsonable_encoder(_status(self._shared))})
            await ws.send_json({"event": "stats",     "data": jsonable_encoder(_stats(self._shared))})
            await ws.send_json({"event": "markets",   "data": jsonable_encoder(_markets(self._shared))})
            await ws.send_json({"event": "positions", "data": jsonable_encoder(_positions(self._shared))})
            await ws.send_json({"event": "history",   "data": jsonable_encoder(_history(self._shared))})
            await ws.send_json({"event": "logs",      "data": jsonable_encoder(_logs(self._shared, 100))})
        except Exception as exc:
            logger.warning("Ошибка отправки снимка клиенту: %s", exc)

    # ── Периодические пуши ────────────────────────────────────────────────

    async def _push_periodic(self) -> None:
        await self.broadcast("status",   _status(self._shared))
        await self.broadcast("markets",  _markets(self._shared))
        await self._push_new_logs()

    async def _push_new_logs(self) -> None:
        """Пушит только новые записи лога с момента последнего пуша."""
        all_logs = self._shared.get_logs(200)
        if self._log_cursor > len(all_logs):
            self._log_cursor = 0  # буфер перемотался
        new_logs = all_logs[self._log_cursor:]
        if new_logs:
            self._log_cursor = len(all_logs)
            await self.broadcast("logs_append", _encode_list(new_logs))


# ── Вспомогательные функции ───────────────────────────────────────────────

def _status(shared: SharedState) -> dict:
    return dataclasses.asdict(shared.get_status())


def _stats(shared: SharedState) -> dict:
    return dataclasses.asdict(shared.get_stats())


def _markets(shared: SharedState) -> list:
    return [_market_summary(m) for m in shared.get_all_markets()]


def _positions(shared: SharedState) -> list:
    return _encode_list(shared.get_all_positions())


def _history(shared: SharedState) -> list:
    return _encode_list(shared.get_history(50))


def _logs(shared: SharedState, limit: int) -> list:
    return _encode_list(shared.get_logs(limit))


def _market_summary(market: MarketState) -> dict:
    """Краткое представление рынка с последними N точками истории."""
    latest = market.prob_history[-1] if market.prob_history else None
    short_history = [dataclasses.asdict(p) for p in market.prob_history[-_HISTORY_IN_MARKETS:]]
    return {
        "condition_id": market.condition_id,
        "slug": market.slug,
        "question": market.question,
        "series_ticker": market.series_ticker,
        "close_time": market.close_time.isoformat(),
        "start_time": market.start_time.isoformat(),
        "status": market.status,
        "signal_fired": market.signal_fired,
        "outcomes": market.outcomes,
        "latest_prob": latest.probability if latest else None,
        "latest_prob_ts": latest.timestamp.isoformat() if latest else None,
        "prob_history": short_history,
    }


def _encode_list(items: list) -> list:
    return [dataclasses.asdict(x) for x in items]
