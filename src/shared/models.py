"""Общие модели данных для всех модулей LizardBot."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ProbPoint:
    """Точка истории вероятности рынка."""

    timestamp: datetime
    probability: float  # 0.0 – 1.0


@dataclass
class MarketState:
    """Состояние отслеживаемого рынка Polymarket."""

    condition_id: str           # conditionId — ключ для CLOB API
    slug: str                   # URL-slug для Gamma API
    question: str               # Текст вопроса рынка
    series_ticker: str          # e.g. "btc-up-or-down-4h"
    start_time: datetime        # Начало ценового окна (eventStartTime)
    close_time: datetime        # Время закрытия/резолюции рынка
    created_at: datetime        # Время добавления в мониторинг
    outcomes: List[str]         # e.g. ["Up", "Down"]
    token_ids: Dict[str, str]   # outcome -> clobTokenId, e.g. {"Up": "token1"}
    order_min_size: float       # Минимальный размер ставки (USDC)
    neg_risk: bool              # Флаг negRisk для CLOB API
    prob_history: List[ProbPoint] = field(default_factory=list)
    signal_fired: bool = False
    signal_time: Optional[datetime] = None
    status: str = "monitoring"  # monitoring | bet_placed | closed | skipped


@dataclass
class Position:
    """Открытая позиция на рынке."""

    condition_id: str
    outcome: str
    amount: float
    entry_price: float
    entry_time: datetime
    simulation: bool
    order_id: Optional[str] = None


@dataclass
class Trade:
    """Закрытая сделка (итог позиции)."""

    condition_id: str
    question: str
    outcome: str
    amount: float
    entry_price: float
    entry_time: datetime
    close_time: datetime
    result: str         # won | lost
    pnl: float
    simulation: bool


@dataclass
class LogEntry:
    """Запись лога для отображения на фронтенде."""

    timestamp: datetime
    level: str          # DEBUG | INFO | WARNING | ERROR
    thread: str
    message: str


@dataclass
class BotStatus:
    """Текущий статус бота."""

    running: bool
    simulation_mode: bool
    started_at: Optional[datetime]
    markets_monitored: int
    open_positions: int
    total_trades: int
    win_rate: float
    total_pnl: float
    balance: float


@dataclass
class Command:
    """Команда от фронтенда к боту."""

    action: str                             # start | stop | reload_config
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StatsSnapshot:
    """Снимок статистики для фронтенда."""

    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    roi: float
    max_drawdown: float
    balance: float
    simulation_mode: bool


@dataclass
class SignalResult:
    """Результат проверки торгового сигнала стратегии."""

    should_trade: bool
    outcome: Optional[str]
    probability: Optional[float]
    volatility: Optional[float]
    reason: str
    is_danger_zone: bool = False  # prob в диапазоне 0.80–0.90
