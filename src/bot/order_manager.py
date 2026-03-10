"""OrderManager — расчёт ставок, размещение ордеров, закрытие позиций."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from src.client.polymarket import PolymarketClient, PolymarketOrderError
from src.config.models import BotConfig
from src.db.repository import DBRepository
from src.shared.models import MarketState, Position, SignalResult, Trade
from src.shared.state import SharedState

logger = logging.getLogger(__name__)

# Приблизительная комиссия taker на Polymarket (уточняется по факту)
_TAKER_FEE_RATE = 0.01


class OrderManager:
    """Управляет жизненным циклом ставок: вход → ожидание → закрытие.

    В режиме simulation_mode=True сделки эмулируются без реальных ордеров.
    """

    def __init__(
        self,
        client: PolymarketClient,
        shared: SharedState,
        db: DBRepository,
    ) -> None:
        self._client = client
        self._shared = shared
        self._db = db
        self._initial_balance: Optional[float] = None

    def set_initial_balance(self, balance: float) -> None:
        """Устанавливает начальный баланс для расчёта double_on_double.

        Вызывается один раз при старте BotEngine.
        """
        if self._initial_balance is None:
            self._initial_balance = balance
            logger.info("OrderManager: начальный баланс = %.2f USDC", balance)

    def calculate_bet_amount(self, config: BotConfig, signal: SignalResult) -> float:
        """Рассчитывает размер ставки согласно bet_mode из конфига.

        Режимы:
        - fixed: config.bet_amount (фиксированная сумма)
        - percent: balance * bet_percent / 100
        - double_on_double: удваивает при удвоении баланса
        """
        balance = self._shared.get_balance()
        amount = self._calc_by_mode(config, balance)

        if signal.is_danger_zone and config.danger_zone_action == "reduce":
            amount *= config.danger_zone_reduce_factor
            logger.debug("Ставка снижена (danger zone): %.2f USDC", amount)

        return round(amount, 2)

    def place_bet(
        self, market: MarketState, signal: SignalResult, config: BotConfig
    ) -> None:
        """Размещает ставку (реальную или эмулированную).

        После успешного размещения:
        - Обновляет SharedState и БД
        - Выставляет market.status = 'bet_placed'
        - Выставляет market.signal_fired = True
        """
        amount = self.calculate_bet_amount(config, signal)
        token_id = market.token_ids.get(signal.outcome or "")
        entry_price = signal.probability or 0.5

        position = Position(
            condition_id=market.condition_id,
            outcome=signal.outcome or "",
            amount=amount,
            entry_price=entry_price,
            entry_time=datetime.now(timezone.utc),
            simulation=config.simulation_mode,
        )

        if config.simulation_mode:
            self._log_sim_bet(market, signal, amount)
        else:
            self._place_real_order(position, market, token_id, amount, config)

        market.signal_fired = True
        market.signal_time = position.entry_time
        self._shared.update_market(market)
        self._shared.add_position(position)
        self._shared.set_market_status(market.condition_id, "bet_placed")
        self._db.save_position(position)
        self._db.save_market(market)

    def settle(self, condition_id: str, result: str, config: BotConfig) -> None:
        """Закрывает позицию по рынку с известным результатом.

        Args:
            condition_id: ID рынка.
            result: Победивший исход из Polymarket (e.g. "Up").
            config: Снимок конфига текущей итерации.
        """
        position = self._shared.get_position(condition_id)
        if position is None:
            self._shared.set_market_status(condition_id, "closed")
            self._db.update_market_status(condition_id, "closed")
            return

        market = self._shared.get_market(condition_id)
        question = market.question if market else condition_id[:16]
        won = (result == position.outcome)
        pnl = self._calc_pnl(position, won)

        trade = Trade(
            condition_id=condition_id,
            question=question,
            outcome=position.outcome,
            amount=position.amount,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            close_time=datetime.now(timezone.utc),
            result="won" if won else "lost",
            pnl=pnl,
            simulation=position.simulation,
        )

        self._shared.settle_position(condition_id, trade)
        self._shared.set_market_status(condition_id, "closed")
        self._db.save_trade(trade)
        self._db.close_position(condition_id)
        self._db.update_market_status(condition_id, "closed")

        sim_tag = " [SIM]" if position.simulation else ""
        logger.info(
            "Сделка закрыта%s: %s | %s | PnL: %+.2f USDC | ставка: %s @ %.3f",
            sim_tag, question, "WIN" if won else "LOSS",
            pnl, position.outcome, position.entry_price,
        )

    # ── Приватные методы ──────────────────────────────────────────────────

    def _calc_by_mode(self, config: BotConfig, balance: float) -> float:
        """Вычисляет базовый размер ставки по bet_mode."""
        if config.bet_mode == "percent":
            return balance * config.bet_percent / 100

        if config.bet_mode == "double_on_double":
            return self._double_on_double(config, balance)

        return config.bet_amount  # "fixed"

    def _double_on_double(self, config: BotConfig, balance: float) -> float:
        """Удваивает ставку при каждом удвоении баланса относительно старта."""
        initial = self._initial_balance or balance
        if initial <= 0 or balance <= 0:
            return config.bet_amount
        n = max(0, math.floor(math.log2(balance / initial)))
        return config.bet_amount * (2 ** n)

    def _calc_pnl(self, position: Position, won: bool) -> float:
        """Вычисляет PnL по закрытой позиции."""
        if not won:
            return -position.amount
        gross = (1.0 / position.entry_price - 1.0) * position.amount
        fee = position.amount * _TAKER_FEE_RATE
        return round(gross - fee, 4)

    def _place_real_order(
        self,
        position: Position,
        market: MarketState,
        token_id: Optional[str],
        amount: float,
        config: BotConfig,
    ) -> None:
        """Отправляет реальный ордер через CLOB API."""
        if not token_id:
            raise PolymarketOrderError(
                f"Нет token_id для исхода {position.outcome} рынка {market.condition_id[:16]}"
            )
        resp = self._client.place_order(
            token_id=token_id,
            amount=amount,
            neg_risk=market.neg_risk,
            order_min_size=market.order_min_size,
        )
        position.order_id = resp.get("orderID")
        logger.info(
            "Ордер размещён: %s | %s | %.2f USDC @ %.3f | order_id=%s",
            market.question, position.outcome,
            amount, position.entry_price, position.order_id,
        )

    def _log_sim_bet(
        self, market: MarketState, signal: SignalResult, amount: float
    ) -> None:
        logger.info(
            "[SIM] Ставка: %s | %s | %.2f USDC @ %.3f | vol=%.3f",
            market.question, signal.outcome,
            amount, signal.probability or 0, signal.volatility or 0,
        )
