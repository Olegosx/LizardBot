"""MarketTracker — опрос вероятностей и детектирование закрытых рынков (часть Thread 1)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

from src.client.polymarket import PolymarketClient, PolymarketNetworkError
from src.config.models import BotConfig
from src.shared.state import SharedState

logger = logging.getLogger(__name__)


class MarketTracker:
    """Периодически обновляет prob_history всех отслеживаемых рынков.

    На каждой итерации BotEngine:
    1. poll_all() — запрашивает текущую вероятность у каждого рынка
    2. get_closed_ids() — возвращает condition_id рынков, время которых вышло
    """

    def __init__(self, client: PolymarketClient, shared: SharedState) -> None:
        self._client = client
        self._shared = shared

    def poll_all(self, config: BotConfig) -> None:
        """Обновляет prob_history для всех рынков в статусе monitoring или bet_placed."""
        market_ids = self._shared.get_monitored_condition_ids()
        for condition_id in market_ids:
            market = self._shared.get_market(condition_id)
            if market is None or market.status in ("closed", "skipped"):
                continue
            self._poll_one(condition_id, market.token_ids, market.outcomes)

    def _poll_one(
        self,
        condition_id: str,
        token_ids: dict,
        outcomes: List[str],
    ) -> None:
        """Запрашивает вероятность первого исхода и пишет в prob_history."""
        if not outcomes:
            return
        primary_outcome = outcomes[0]
        token_id = token_ids.get(primary_outcome)
        if not token_id:
            logger.warning("Нет token_id для исхода %s рынка %s", primary_outcome, condition_id[:16])
            return

        probability = self._client.get_market_probability(token_id)
        if probability is None:
            logger.debug("Нет котировки для рынка %s", condition_id[:16])
            return

        self._shared.append_prob_point(condition_id, probability)
        logger.debug("Рынок %s: P(%s)=%.3f", condition_id[:16], primary_outcome, probability)

    def get_closed_ids(self) -> List[str]:
        """Возвращает condition_id рынков, у которых close_time <= now.

        Детектирование по времени — основной механизм.
        Финальный результат запрашивается отдельно через PolymarketClient.
        """
        now = datetime.now(timezone.utc)
        closed: List[str] = []
        for market in self._shared.get_all_markets():
            if market.status in ("closed", "skipped"):
                continue
            if market.close_time <= now:
                closed.append(market.condition_id)
                logger.info(
                    "Рынок закрыт по времени: %s (%s)",
                    market.question,
                    market.condition_id[:16],
                )
        return closed

    def fetch_result(self, condition_id: str) -> None:
        """Запрашивает результат закрытого рынка через Gamma API.

        Результат не возвращается напрямую — используй
        PolymarketClient.get_market_result(slug) из OrderManager.
        """
        market = self._shared.get_market(condition_id)
        if market is None:
            return
        try:
            result = self._client.get_market_result(market.slug)
            if result:
                logger.info("Рынок %s: результат = %s", market.question, result)
            else:
                logger.info("Рынок %s: результат ещё не опубликован", market.question)
        except PolymarketNetworkError as exc:
            logger.error("Не удалось получить результат рынка %s: %s", condition_id[:16], exc)
