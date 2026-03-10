"""Тесты LizardStrategy."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import patch

import pytest

from src.bot.strategy import LizardStrategy, _DANGER_ZONE_HIGH, _DANGER_ZONE_LOW
from src.config.models import BotConfig
from src.shared.models import MarketState, ProbPoint


# ── Фабрики ──────────────────────────────────────────────────────────────────

def make_config(**kwargs) -> BotConfig:
    defaults = dict(
        private_key="0x1",
        api_key="", api_secret="", api_passphrase="",
        funder_address="0x2",
        simulation_mode=True,
        vol_threshold=0.20,
        lookback_minutes=30,
        danger_zone_action="skip",
        danger_zone_reduce_factor=0.5,
    )
    defaults.update(kwargs)
    return BotConfig(**defaults)


def make_market(
    *,
    minutes_to_close: float = 15.0,
    prob_history: List[float] | None = None,
    signal_fired: bool = False,
) -> MarketState:
    now = datetime.now(timezone.utc)
    close_time = now + timedelta(minutes=minutes_to_close)

    if prob_history is None:
        probs = [0.95] * 5
    else:
        probs = prob_history

    history = [
        ProbPoint(
            timestamp=now - timedelta(minutes=len(probs) - i),
            probability=p,
        )
        for i, p in enumerate(probs)
    ]

    return MarketState(
        condition_id="cid_test",
        slug="btc-up-down-test",
        question="BTC up or down?",
        series_ticker="btc-up-or-down-4h",
        start_time=now - timedelta(hours=3),
        close_time=close_time,
        created_at=now,
        outcomes=["Up", "Down"],
        token_ids={"Up": "tok1", "Down": "tok2"},
        order_min_size=1.0,
        neg_risk=False,
        prob_history=history,
        signal_fired=signal_fired,
    )


# ── check_signal ──────────────────────────────────────────────────────────────

class TestCheckSignal:
    strategy = LizardStrategy()

    def test_signal_already_fired(self):
        market = make_market(signal_fired=True)
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is False
        assert "уже" in result.reason

    def test_too_early(self):
        market = make_market(minutes_to_close=60.0)
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is False
        assert "рано" in result.reason

    def test_already_closed(self):
        market = make_market(minutes_to_close=-1.0)
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is False
        assert "закрыт" in result.reason

    def test_no_history(self):
        market = make_market(prob_history=[0.95])  # только 1 точка
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is False
        assert "недостаточно" in result.reason

    def test_high_volatility(self):
        # Чередование 0.2/0.8 даёт stdev ≈ 0.30 > 0.20
        alternating = [0.2, 0.8] * 10
        market = make_market(prob_history=alternating)
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is False
        assert "волатильность" in result.reason

    def test_signal_confirmed(self):
        # Стабильная история 0.95 → low vol → сигнал
        market = make_market(prob_history=[0.95] * 10)
        result = self.strategy.check_signal(market, make_config())
        assert result.should_trade is True
        assert result.outcome == "Up"
        assert abs(result.probability - 0.95) < 1e-9

    def test_danger_zone_skip(self):
        market = make_market(prob_history=[0.85] * 10)
        result = self.strategy.check_signal(market, make_config(danger_zone_action="skip"))
        assert result.should_trade is False
        assert "опасная зона" in result.reason

    def test_danger_zone_reduce(self):
        market = make_market(prob_history=[0.85] * 10)
        result = self.strategy.check_signal(market, make_config(danger_zone_action="reduce"))
        assert result.should_trade is True
        assert result.is_danger_zone is True

    def test_danger_zone_trade(self):
        market = make_market(prob_history=[0.85] * 10)
        result = self.strategy.check_signal(market, make_config(danger_zone_action="trade"))
        assert result.should_trade is True
        assert result.is_danger_zone is True


# ── get_leading_outcome ───────────────────────────────────────────────────────

class TestGetLeadingOutcome:
    strategy = LizardStrategy()
    outcomes = ["Up", "Down"]

    def test_first_outcome_leads(self):
        outcome, prob = self.strategy.get_leading_outcome(0.70, self.outcomes)
        assert outcome == "Up"
        assert abs(prob - 0.70) < 1e-9

    def test_second_outcome_leads(self):
        outcome, prob = self.strategy.get_leading_outcome(0.30, self.outcomes)
        assert outcome == "Down"
        assert abs(prob - 0.70) < 1e-9

    def test_exactly_half(self):
        # 0.5 → первый исход
        outcome, _ = self.strategy.get_leading_outcome(0.50, self.outcomes)
        assert outcome == "Up"


# ── calculate_volatility ─────────────────────────────────────────────────────

class TestCalculateVolatility:
    strategy = LizardStrategy()

    def test_returns_none_for_single_point(self):
        now = datetime.now(timezone.utc)
        history = [ProbPoint(now, 0.8)]
        assert self.strategy.calculate_volatility(history, 30) is None

    def test_returns_none_for_empty(self):
        assert self.strategy.calculate_volatility([], 30) is None

    def test_correct_stdev(self):
        now = datetime.now(timezone.utc)
        values = [0.90, 0.92, 0.88, 0.91]
        history = [
            ProbPoint(now - timedelta(minutes=len(values) - i), v)
            for i, v in enumerate(values)
        ]
        result = self.strategy.calculate_volatility(history, 30)
        expected = statistics.stdev(values)
        assert abs(result - expected) < 1e-9

    def test_ignores_old_points(self):
        now = datetime.now(timezone.utc)
        # Одна старая точка (за 60 мин), одна новая
        old = ProbPoint(now - timedelta(minutes=60), 0.50)
        recent = ProbPoint(now - timedelta(minutes=5), 0.95)
        result = self.strategy.calculate_volatility([old, recent], 30)
        # Только 1 точка в окне → None
        assert result is None


# ── is_danger_zone ────────────────────────────────────────────────────────────

class TestIsDangerZone:
    strategy = LizardStrategy()

    @pytest.mark.parametrize("prob,expected", [
        (0.79, False),
        (0.80, True),
        (0.85, True),
        (0.90, True),
        (0.91, False),
        (0.95, False),
    ])
    def test_boundaries(self, prob: float, expected: bool):
        assert self.strategy.is_danger_zone(prob) == expected
