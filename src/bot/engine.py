"""BotEngine — главный цикл бота (Thread 1)."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from src.bot.order_manager import OrderManager
from src.bot.scanner import MarketScanner
from src.bot.strategy import LizardStrategy
from src.bot.tracker import MarketTracker
from src.client.polymarket import PolymarketClient
from src.config.models import BotConfig
from src.db.repository import DBRepository
from src.shared.models import MarketState, SignalResult
from src.shared.state import SharedState

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # секунд между итерациями основного цикла


class BotEngine:
    """Оркестрирует работу бота: сканирование, трекинг, стратегия, ордера.

    Запускается как Thread 1. Останавливается через stop() или команду 'stop'
    из SharedState.commands.
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
        self._strategy = LizardStrategy()
        self._scanner = MarketScanner(client)
        self._tracker = MarketTracker(client, shared)
        self._order_manager = OrderManager(client, shared, db)
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Сигнализирует основному циклу об остановке."""
        self._stop_event.set()

    def run(self) -> None:
        """Главный цикл Thread 1.

        Поток всегда работает. Торговая логика выполняется только когда
        config.active == True. Запуск/стоп — через поле active в config.json.
        """
        logger.info("BotEngine запущен")
        self._recover_state()

        while not self._stop_event.is_set():
            config = self._shared.get_config_snapshot()
            self._shared.set_running(config.active)

            if config.active:
                try:
                    self._tick(config)
                except Exception as exc:
                    logger.error("Ошибка в основном цикле: %s", exc, exc_info=True)

            self._stop_event.wait(timeout=POLL_INTERVAL)

        self._shared.set_running(False)
        logger.info("BotEngine остановлен")

    # ── Одна итерация ─────────────────────────────────────────────────────

    def _tick(self, config: BotConfig) -> None:
        """Выполняет одну итерацию основного цикла."""
        self._handle_commands()
        self._scan_new_markets(config)
        self._tracker.poll_all(config)

        for condition_id in self._shared.get_monitored_condition_ids():
            market = self._shared.get_market(condition_id)
            if market:
                self._process_market(market, config)

        for condition_id in self._tracker.get_closed_ids():
            self._settle_market(condition_id, config)

        self._update_balance(config)

    def _scan_new_markets(self, config: BotConfig) -> None:
        """Ищет новые рынки и регистрирует их в SharedState и БД."""
        new_markets = self._scanner.scan(config)
        for market in new_markets:
            self._shared.add_market(market)
            self._scanner.mark_tracked(market.condition_id)
            self._db.save_market(market)

    # ── Обработка рынка ───────────────────────────────────────────────────

    def _process_market(self, market: MarketState, config: BotConfig) -> None:
        """Проверяет сигнал и при необходимости размещает ставку."""
        if market.status != "monitoring":
            return

        signal = self._strategy.check_signal(market, config)

        if not signal.should_trade and "недостаточно истории" in signal.reason:
            signal = self._apply_recovery_action(market, config) or signal

        if signal.should_trade:
            logger.info(
                "Сигнал: %s | %s @ %.3f | vol=%.3f",
                market.question, signal.outcome,
                signal.probability or 0, signal.volatility or 0,
            )
            self._order_manager.place_bet(market, signal, config)
        else:
            logger.debug("Нет сигнала [%s]: %s", market.condition_id[:16], signal.reason)

    def _apply_recovery_action(
        self, market: MarketState, config: BotConfig
    ) -> Optional[SignalResult]:
        """Обрабатывает рынок с пропущенным окном (бот был офлайн).

        Применяется только если мы в T-30 окне, но нет истории вероятностей.
        """
        minutes_left = (market.close_time - datetime.now(timezone.utc)).total_seconds() / 60
        if minutes_left > config.lookback_minutes or minutes_left <= 0:
            return None

        action = config.recovery_action
        if action == "skip":
            logger.info("[recovery=skip] Пропускаем %s (нет истории)", market.question)
            self._shared.set_market_status(market.condition_id, "skipped")
            self._db.update_market_status(market.condition_id, "skipped")
            return None

        if not market.prob_history:
            return None  # Ждём первых данных от трекера

        prob = market.prob_history[-1].probability
        outcome, leading_prob = self._strategy.get_leading_outcome(prob, market.outcomes)

        if action == "enter_if_safe" and self._strategy.is_danger_zone(leading_prob):
            logger.info(
                "[recovery=enter_if_safe] Опасная зона %.3f, пропускаем %s",
                leading_prob, market.question,
            )
            return None

        logger.info("[recovery=%s] Входим: %s @ %.3f", action, market.question, leading_prob)
        return SignalResult(True, outcome, leading_prob, None, f"recovery={action}")

    def _settle_market(self, condition_id: str, config: BotConfig) -> None:
        """Запрашивает результат закрытого рынка и закрывает позицию."""
        market = self._shared.get_market(condition_id)
        if market is None or market.status == "closed":
            return

        result = self._client.get_market_result(market.slug)
        if result:
            self._order_manager.settle(condition_id, result, config)
        else:
            logger.debug("Результат рынка %s ещё не доступен", condition_id[:16])

    # ── Восстановление после рестарта ─────────────────────────────────────

    def _recover_state(self) -> None:
        """Восстанавливает состояние из БД при старте."""
        markets = self._db.load_active_markets()
        positions = self._db.load_open_positions()
        position_ids = {p.condition_id for p in positions}

        for market in markets:
            self._shared.add_market(market)
            self._scanner.mark_tracked(market.condition_id)
        for position in positions:
            self._shared.add_position(position)

        logger.info(
            "Восстановлено из БД: %d рынков, %d позиций",
            len(markets), len(positions),
        )
        self._recover_closed_markets(position_ids)

    def _recover_closed_markets(self, position_ids: set) -> None:
        """Обрабатывает рынки, закрывшиеся пока бот был офлайн."""
        config = self._shared.get_config_snapshot()
        for condition_id in self._tracker.get_closed_ids():
            if condition_id in position_ids:
                self._settle_market(condition_id, config)
            else:
                self._shared.set_market_status(condition_id, "closed")
                self._db.update_market_status(condition_id, "closed")

    # ── Вспомогательные ───────────────────────────────────────────────────

    def _handle_commands(self) -> None:
        """Обрабатывает служебные команды из очереди SharedState.commands.

        Запуск/стоп стратегии управляется через config.active, а не командами.
        """
        while True:
            cmd = self._shared.pop_command()
            if cmd is None:
                break
            logger.debug("Команда получена: %s %s", cmd.action, cmd.params)

    def _update_balance(self, config: BotConfig) -> None:
        """Запрашивает баланс USDC и обновляет SharedState + БД."""
        if config.simulation_mode:
            if self._shared.get_balance() == 0.0:
                self._shared.update_balance(100.0)
                self._order_manager.set_initial_balance(100.0)
            return
        try:
            balance = self._client.get_balance()
            self._shared.update_balance(balance)
            self._db.save_balance(balance, datetime.now(timezone.utc))
            self._order_manager.set_initial_balance(balance)
        except Exception as exc:
            logger.warning("Не удалось получить баланс: %s", exc)
