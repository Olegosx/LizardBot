"""SharedState — единственный канал коммуникации между потоками."""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from queue import Queue
from typing import Dict, List, Optional

from src.config.models import BotConfig
from src.shared.models import (
    BotStatus,
    Command,
    LogEntry,
    MarketState,
    Position,
    StatsSnapshot,
    Trade,
)

logger = logging.getLogger(__name__)

_LOG_BUFFER_SIZE = 500


class SharedState:
    """Потокобезопасное хранилище данных, общих для всех потоков.

    Все поля защищены одним RLock. Потоки НЕ обращаются к полям напрямую,
    только через публичные методы этого класса.

    Перед началом каждой итерации поток вызывает get_config_snapshot()
    и работает с локальной копией конфига — чтобы не поймать обновление
    от ConfigLoader посередине цикла.
    """

    def __init__(self, config: BotConfig) -> None:
        self._lock = threading.RLock()
        self._config = config
        self._markets: Dict[str, MarketState] = {}          # condition_id -> MarketState
        self._positions: Dict[str, Position] = {}           # condition_id -> Position
        self._history: List[Trade] = []
        self._log_buffer: deque[LogEntry] = deque(maxlen=_LOG_BUFFER_SIZE)
        self._commands: Queue[Command] = Queue()
        self._balance: float = 0.0
        self._status = BotStatus(
            running=False,
            simulation_mode=config.simulation_mode,
            started_at=None,
            markets_monitored=0,
            open_positions=0,
            total_trades=0,
            win_rate=0.0,
            total_pnl=0.0,
            balance=0.0,
        )

    # ── Конфиг ────────────────────────────────────────────────────────────

    def get_config_snapshot(self) -> BotConfig:
        """Возвращает копию конфига для использования внутри одной итерации."""
        with self._lock:
            return self._config

    def update_config(self, config: BotConfig) -> None:
        """Обновляет конфиг (вызывается ConfigLoader из Thread 3)."""
        with self._lock:
            self._config = config
            self._status.simulation_mode = config.simulation_mode
        logger.info("SharedState: конфиг обновлён")

    # ── Рынки ─────────────────────────────────────────────────────────────

    def add_market(self, market: MarketState) -> None:
        """Добавляет новый рынок в мониторинг."""
        with self._lock:
            self._markets[market.condition_id] = market
            self._status.markets_monitored = len(self._markets)

    def get_market(self, condition_id: str) -> Optional[MarketState]:
        """Возвращает рынок по condition_id или None."""
        with self._lock:
            return self._markets.get(condition_id)

    def get_all_markets(self) -> List[MarketState]:
        """Возвращает список всех отслеживаемых рынков."""
        with self._lock:
            return list(self._markets.values())

    def get_monitored_condition_ids(self) -> List[str]:
        """Возвращает список condition_id всех отслеживаемых рынков."""
        with self._lock:
            return list(self._markets.keys())

    def update_market(self, market: MarketState) -> None:
        """Обновляет данные рынка (prob_history, status и т.д.)."""
        with self._lock:
            self._markets[market.condition_id] = market

    def append_prob_point(self, condition_id: str, probability: float) -> None:
        """Добавляет точку истории вероятности к рынку."""
        from src.shared.models import ProbPoint
        with self._lock:
            market = self._markets.get(condition_id)
            if market is None:
                return
            market.prob_history.append(
                ProbPoint(timestamp=datetime.now(timezone.utc), probability=probability)
            )

    def set_market_status(self, condition_id: str, status: str) -> None:
        """Обновляет статус рынка."""
        with self._lock:
            market = self._markets.get(condition_id)
            if market:
                market.status = status

    # ── Позиции ───────────────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        """Записывает открытую позицию."""
        with self._lock:
            self._positions[position.condition_id] = position
            self._status.open_positions = len(self._positions)

    def get_position(self, condition_id: str) -> Optional[Position]:
        """Возвращает открытую позицию или None."""
        with self._lock:
            return self._positions.get(condition_id)

    def get_all_positions(self) -> List[Position]:
        """Возвращает все открытые позиции."""
        with self._lock:
            return list(self._positions.values())

    def settle_position(self, condition_id: str, trade: Trade) -> None:
        """Закрывает позицию и добавляет сделку в историю."""
        with self._lock:
            self._positions.pop(condition_id, None)
            self._history.append(trade)
            self._status.open_positions = len(self._positions)
            self._status.total_trades += 1
            self._status.total_pnl += trade.pnl
            self._recalc_win_rate()

    def _recalc_win_rate(self) -> None:
        """Пересчитывает winrate. Вызывать только под _lock."""
        total = self._status.total_trades
        if total == 0:
            self._status.win_rate = 0.0
            return
        wins = sum(1 for t in self._history if t.result == "won")
        self._status.win_rate = wins / total

    # ── История ───────────────────────────────────────────────────────────

    def get_history(self, limit: int = 100) -> List[Trade]:
        """Возвращает последние сделки."""
        with self._lock:
            return list(self._history[-limit:])

    # ── Баланс ────────────────────────────────────────────────────────────

    def update_balance(self, balance: float) -> None:
        """Обновляет баланс USDC."""
        with self._lock:
            self._balance = balance
            self._status.balance = balance

    def get_balance(self) -> float:
        with self._lock:
            return self._balance

    # ── Статус бота ───────────────────────────────────────────────────────

    def set_running(self, running: bool) -> None:
        """Устанавливает флаг работы бота (отражает config.active)."""
        with self._lock:
            was_running = self._status.running
            self._status.running = running
            if running and not was_running:
                self._status.started_at = datetime.now(timezone.utc)

    def get_status(self) -> BotStatus:
        """Возвращает копию текущего статуса бота."""
        with self._lock:
            return BotStatus(
                running=self._status.running,
                simulation_mode=self._status.simulation_mode,
                started_at=self._status.started_at,
                markets_monitored=self._status.markets_monitored,
                open_positions=self._status.open_positions,
                total_trades=self._status.total_trades,
                win_rate=self._status.win_rate,
                total_pnl=self._status.total_pnl,
                balance=self._status.balance,
            )

    def get_stats(self) -> StatsSnapshot:
        """Возвращает агрегированную статистику для фронтенда."""
        with self._lock:
            wins = sum(1 for t in self._history if t.result == "won")
            losses = len(self._history) - wins
            pnl_values = [t.pnl for t in self._history]
            max_drawdown = _calc_max_drawdown(pnl_values)
            initial = sum(t.amount for t in self._history) or 1.0
            return StatsSnapshot(
                total_trades=len(self._history),
                wins=wins,
                losses=losses,
                win_rate=self._status.win_rate,
                total_pnl=self._status.total_pnl,
                roi=self._status.total_pnl / initial * 100,
                max_drawdown=max_drawdown,
                balance=self._balance,
                simulation_mode=self._status.simulation_mode,
            )

    # ── Лог-буфер ─────────────────────────────────────────────────────────

    def add_log(self, entry: LogEntry) -> None:
        """Добавляет запись в лог-буфер для фронтенда."""
        with self._lock:
            self._log_buffer.append(entry)

    def get_logs(self, limit: int = 200) -> List[LogEntry]:
        """Возвращает последние записи лога."""
        with self._lock:
            entries = list(self._log_buffer)
            return entries[-limit:]

    # ── Команды (frontend → бот) ──────────────────────────────────────────

    def push_command(self, cmd: Command) -> None:
        """Кладёт команду в очередь (вызывается из APIServer)."""
        self._commands.put(cmd)

    def pop_command(self) -> Optional[Command]:
        """Забирает команду из очереди без блокировки. None если пусто."""
        if self._commands.empty():
            return None
        return self._commands.get_nowait()


# ── Вспомогательные функции ───────────────────────────────────────────────

def _calc_max_drawdown(pnl_values: List[float]) -> float:
    """Вычисляет максимальную просадку по последовательности PnL."""
    if not pnl_values:
        return 0.0
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for pnl in pnl_values:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_dd:
            max_dd = drawdown
    return max_dd
