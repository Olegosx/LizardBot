"""Тесты OrderManager: расчёт ставок и PnL."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.bot.order_manager import OrderManager, _TAKER_FEE_RATE
from src.config.models import BotConfig
from src.shared.models import MarketState, Position, SignalResult


# ── Фабрики ───────────────────────────────────────────────────────────────────

def make_config(**kwargs) -> BotConfig:
    defaults = dict(
        private_key="0x1", api_key="", api_secret="",
        api_passphrase="", funder_address="0x2",
        simulation_mode=True,
        bet_mode="fixed", bet_amount=10.0, bet_percent=5.0,
        danger_zone_action="skip", danger_zone_reduce_factor=0.5,
    )
    defaults.update(kwargs)
    return BotConfig(**defaults)


def make_signal(prob=0.95, outcome="Up", is_danger_zone=False) -> SignalResult:
    return SignalResult(
        should_trade=True,
        outcome=outcome,
        probability=prob,
        volatility=0.05,
        reason="ok",
        is_danger_zone=is_danger_zone,
    )


def make_position(amount=10.0, entry_price=0.95, won_outcome="Up") -> Position:
    return Position(
        condition_id="cid",
        outcome=won_outcome,
        amount=amount,
        entry_price=entry_price,
        entry_time=datetime.now(timezone.utc),
        simulation=True,
    )


def make_shared(balance=100.0) -> MagicMock:
    shared = MagicMock()
    shared.get_balance.return_value = balance
    return shared


def make_om(balance=100.0) -> OrderManager:
    return OrderManager(
        client=MagicMock(),
        shared=make_shared(balance),
        db=MagicMock(),
    )


# ── calculate_bet_amount ──────────────────────────────────────────────────────

class TestCalculateBetAmount:
    def test_fixed_mode(self):
        om = make_om()
        cfg = make_config(bet_mode="fixed", bet_amount=10.0)
        assert om.calculate_bet_amount(cfg, make_signal()) == 10.0

    def test_percent_mode(self):
        om = make_om(balance=200.0)
        cfg = make_config(bet_mode="percent", bet_percent=5.0)
        assert om.calculate_bet_amount(cfg, make_signal()) == 10.0

    def test_double_on_double_no_growth(self):
        om = make_om(balance=100.0)
        om.set_initial_balance(100.0)
        cfg = make_config(bet_mode="double_on_double", bet_amount=5.0)
        assert om.calculate_bet_amount(cfg, make_signal()) == 5.0

    def test_double_on_double_doubled_balance(self):
        om = make_om(balance=200.0)
        om.set_initial_balance(100.0)
        cfg = make_config(bet_mode="double_on_double", bet_amount=5.0)
        assert om.calculate_bet_amount(cfg, make_signal()) == 10.0

    def test_double_on_double_quadrupled_balance(self):
        om = make_om(balance=400.0)
        om.set_initial_balance(100.0)
        cfg = make_config(bet_mode="double_on_double", bet_amount=5.0)
        assert om.calculate_bet_amount(cfg, make_signal()) == 20.0

    def test_danger_zone_reduce(self):
        om = make_om()
        cfg = make_config(bet_mode="fixed", bet_amount=10.0,
                          danger_zone_action="reduce", danger_zone_reduce_factor=0.5)
        signal = make_signal(is_danger_zone=True)
        assert om.calculate_bet_amount(cfg, signal) == 5.0

    def test_danger_zone_no_reduce_when_action_skip(self):
        om = make_om()
        cfg = make_config(bet_mode="fixed", bet_amount=10.0, danger_zone_action="skip")
        signal = make_signal(is_danger_zone=True)
        # action=skip → фактор не применяется
        assert om.calculate_bet_amount(cfg, signal) == 10.0


# ── _calc_pnl ─────────────────────────────────────────────────────────────────

class TestCalcPnl:
    om = make_om()

    def test_loss_returns_minus_amount(self):
        pos = make_position(amount=10.0, entry_price=0.95)
        pnl = self.om._calc_pnl(pos, won=False)
        assert pnl == -10.0

    def test_win_returns_positive_pnl(self):
        pos = make_position(amount=10.0, entry_price=0.95)
        pnl = self.om._calc_pnl(pos, won=True)
        gross = (1 / 0.95 - 1) * 10.0
        fee = 10.0 * _TAKER_FEE_RATE
        assert abs(pnl - round(gross - fee, 4)) < 1e-9

    def test_pnl_is_rounded(self):
        pos = make_position(amount=7.777, entry_price=0.90)
        pnl = self.om._calc_pnl(pos, won=True)
        assert len(str(pnl).split(".")[-1]) <= 4


# ── set_initial_balance ───────────────────────────────────────────────────────

class TestSetInitialBalance:
    def test_sets_once(self):
        om = make_om()
        om.set_initial_balance(100.0)
        om.set_initial_balance(200.0)  # должно игнорироваться
        assert om._initial_balance == 100.0
