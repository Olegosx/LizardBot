"""Сканер рынков: поиск новых рынков для мониторинга (часть Thread 1)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.client.polymarket import PolymarketClient, PolymarketNetworkError
from src.config.models import BotConfig, MarketFilterConfig
from src.shared.models import MarketState

logger = logging.getLogger(__name__)


def _parse_json_field(value: Any) -> list:
    """Парсит JSON-строку или возвращает список как есть."""
    if isinstance(value, str):
        return json.loads(value)
    return value if value is not None else []


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Парсит ISO-строку в datetime (UTC). Возвращает None при ошибке."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class MarketScanner:
    """Ищет новые рынки через Polymarket API и добавляет их в SharedState.

    Вызывается из BotEngine на каждой итерации главного цикла.
    Расширяем: новые типы рынков добавляются через market_filters в конфиге.
    """

    def __init__(self, client: PolymarketClient) -> None:
        self._client = client
        self._tracked: Dict[str, bool] = {}  # condition_id -> True

    def scan(self, config: BotConfig) -> List[MarketState]:
        """Ищет новые рынки по всем включённым фильтрам конфига.

        Returns:
            Список новых MarketState, готовых к добавлению в SharedState.
        """
        new_markets: List[MarketState] = []
        for market_filter in config.market_filters:
            if not market_filter.enabled:
                continue
            new_markets.extend(self._scan_series(market_filter))
        return new_markets

    def mark_tracked(self, condition_id: str) -> None:
        """Помечает рынок как уже отслеживаемый (вызывается из SharedState)."""
        self._tracked[condition_id] = True

    def _scan_series(self, market_filter: MarketFilterConfig) -> List[MarketState]:
        """Сканирует одну серию рынков и возвращает новые."""
        try:
            raw_markets = self._client.get_active_markets(market_filter.series_ticker)
        except PolymarketNetworkError as exc:
            logger.error("Ошибка сканирования серии %s: %s", market_filter.series_ticker, exc)
            return []

        new: List[MarketState] = []
        for raw in raw_markets:
            condition_id = raw.get("conditionId") or str(raw.get("id", ""))
            if not condition_id:
                logger.warning("Рынок без conditionId пропущен: %s", raw.get("slug"))
                continue
            if self._is_tracked(condition_id):
                continue
            if not self._is_tradeable(raw):
                continue

            state = self._to_market_state(raw, market_filter.series_ticker)
            if state is None:
                continue

            new.append(state)
            logger.info(
                "Новый рынок [%s]: %s (закрытие: %s)",
                market_filter.series_ticker,
                state.question,
                state.close_time.strftime("%Y-%m-%d %H:%M UTC"),
            )
        return new

    def _is_tracked(self, condition_id: str) -> bool:
        return condition_id in self._tracked

    def _is_tradeable(self, raw: dict) -> bool:
        """Проверяет что рынок технически пригоден для торговли."""
        if not raw.get("enableOrderBook", False):
            return False
        if raw.get("archived", False):
            return False
        return True

    def _to_market_state(self, raw: dict, series_ticker: str) -> Optional[MarketState]:
        """Конвертирует raw dict из Gamma API → MarketState."""
        outcomes = _parse_json_field(raw.get("outcomes", []))
        token_ids_raw = _parse_json_field(raw.get("clobTokenIds", []))
        if len(outcomes) != len(token_ids_raw) or not outcomes:
            logger.warning("Некорректный маппинг outcomes/tokenIds для %s", raw.get("slug"))
            return None

        close_time = _parse_dt(raw.get("endDate"))
        if close_time is None:
            logger.warning("Рынок без endDate пропущен: %s", raw.get("slug"))
            return None

        # eventStartTime — начало ценового окна; fallback на startDate
        start_time = _parse_dt(raw.get("eventStartTime")) or _parse_dt(raw.get("startDate"))
        if start_time is None:
            start_time = datetime.now(timezone.utc)

        return MarketState(
            condition_id=raw.get("conditionId") or str(raw.get("id", "")),
            slug=raw.get("slug", ""),
            question=raw.get("question", raw.get("title", "")),
            series_ticker=series_ticker,
            start_time=start_time,
            close_time=close_time,
            created_at=datetime.now(timezone.utc),
            outcomes=outcomes,
            token_ids=dict(zip(outcomes, token_ids_raw)),
            order_min_size=float(raw.get("orderMinSize", 5.0)),
            neg_risk=bool(raw.get("negRisk", False)),
        )
