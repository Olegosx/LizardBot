"""Стратегия Hypothesis 1: сигнал за T-30 мин с фильтром волатильности."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from src.config.models import BotConfig
from src.shared.models import MarketState, ProbPoint, SignalResult

# Вероятности в диапазоне 0.80–0.90: комиссия съедает всю прибыль
_DANGER_ZONE_LOW = 0.80
_DANGER_ZONE_HIGH = 0.90


class LizardStrategy:
    """Торговая стратегия LizardBot (оптимальный вариант Гипотезы 1).

    Логика входа в сделку:
    1. За T-30 мин до закрытия определить ведущий исход (prob > 0.5)
    2. Вычислить std dev вероятности за последние 30 мин
    3. Торговать только если std dev <= vol_threshold (0.20)
    4. Применить правила для «опасной зоны» (0.80–0.90)

    ROI при оптимальных параметрах: +9.00% (winrate 95.4%, 474 сделки).
    """

    def check_signal(self, market: MarketState, config: BotConfig) -> SignalResult:
        """Проверяет условия для входа в сделку.

        Returns:
            SignalResult с решением и причиной.
        """
        if market.signal_fired:
            return SignalResult(False, None, None, None, "сигнал уже был выдан")

        minutes_left = self._minutes_to_close(market.close_time)
        if minutes_left > config.lookback_minutes:
            return SignalResult(
                False, None, None, None,
                f"рано: до закрытия {minutes_left:.0f} мин"
            )
        if minutes_left <= 0:
            return SignalResult(False, None, None, None, "рынок уже закрыт")

        volatility = self.calculate_volatility(market.prob_history, config.lookback_minutes)
        if volatility is None:
            return SignalResult(False, None, None, None, "недостаточно истории для расчёта волатильности")

        if volatility > config.vol_threshold:
            return SignalResult(
                False, None, None, volatility,
                f"волатильность {volatility:.3f} > порога {config.vol_threshold}"
            )

        latest_prob = market.prob_history[-1].probability
        outcome, prob = self.get_leading_outcome(latest_prob, market.outcomes)
        in_danger = self.is_danger_zone(prob)

        if in_danger:
            return self._apply_danger_zone_action(outcome, prob, volatility, config)

        return SignalResult(True, outcome, prob, volatility, "сигнал подтверждён")

    def get_leading_outcome(
        self, prob_first: float, outcomes: List[str]
    ) -> Tuple[str, float]:
        """Возвращает (название ведущего исхода, его вероятность).

        prob_first — вероятность outcomes[0] из prob_history.
        """
        if prob_first >= 0.5:
            return outcomes[0], prob_first
        return outcomes[1], 1.0 - prob_first

    def calculate_volatility(
        self, history: List[ProbPoint], window_minutes: int
    ) -> Optional[float]:
        """Вычисляет std dev вероятности за последние window_minutes минут.

        Returns:
            float или None если точек меньше двух.
        """
        window_start = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        points = [p.probability for p in history if p.timestamp >= window_start]
        if len(points) < 2:
            return None
        return statistics.stdev(points)

    def is_danger_zone(self, prob: float) -> bool:
        """True если вероятность в диапазоне 0.80–0.90 (комиссия съедает прибыль)."""
        return _DANGER_ZONE_LOW <= prob <= _DANGER_ZONE_HIGH

    def _apply_danger_zone_action(
        self,
        outcome: str,
        prob: float,
        volatility: float,
        config: BotConfig,
    ) -> SignalResult:
        """Применяет danger_zone_action из конфига."""
        action = config.danger_zone_action
        if action == "skip":
            return SignalResult(
                False, outcome, prob, volatility,
                f"опасная зона p={prob:.3f}: пропускаем (skip)"
            )
        if action == "reduce":
            return SignalResult(
                True, outcome, prob, volatility,
                f"опасная зона p={prob:.3f}: снижаем ставку (reduce)",
                is_danger_zone=True,
            )
        # action == "trade": торгуем без изменений
        return SignalResult(
            True, outcome, prob, volatility,
            f"опасная зона p={prob:.3f}: торгуем (trade)",
            is_danger_zone=True,
        )

    @staticmethod
    def _minutes_to_close(close_time: datetime) -> float:
        """Минут до закрытия рынка (отрицательное если уже закрыт)."""
        return (close_time - datetime.now(timezone.utc)).total_seconds() / 60
