"""DBRepository — персистентное хранилище на SQLite."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from src.shared.models import (
    BotStatus,
    MarketState,
    Position,
    StatsSnapshot,
    Trade,
)

logger = logging.getLogger(__name__)


def _ts(dt: datetime) -> int:
    """datetime → unix timestamp (int)."""
    return int(dt.timestamp())


def _dt(ts: int) -> datetime:
    """unix timestamp → datetime UTC."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


class DBRepository:
    """Потокобезопасный доступ к SQLite через единственное соединение + Lock."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()

    def init_schema(self) -> None:
        """Создаёт таблицы если не существуют."""
        with self._lock:
            self._create_markets_table()
            self._create_positions_table()
            self._create_trades_table()
            self._create_balance_table()
            self._conn.commit()
        logger.info("DB схема инициализирована: %s", self._db_path)

    # ── Markets ───────────────────────────────────────────────────────────

    def save_market(self, market: MarketState) -> None:
        """Сохраняет или обновляет рынок в БД."""
        sql = """
            INSERT INTO markets
                (condition_id, slug, question, series_ticker,
                 start_time, close_time, created_at,
                 outcomes, token_ids, order_min_size, neg_risk,
                 signal_fired, signal_time, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(condition_id) DO UPDATE SET
                status=excluded.status,
                signal_fired=excluded.signal_fired,
                signal_time=excluded.signal_time
        """
        with self._lock:
            self._conn.execute(sql, (
                market.condition_id, market.slug, market.question,
                market.series_ticker,
                _ts(market.start_time), _ts(market.close_time),
                _ts(market.created_at),
                json.dumps(market.outcomes),
                json.dumps(market.token_ids),
                market.order_min_size, int(market.neg_risk),
                int(market.signal_fired),
                _ts(market.signal_time) if market.signal_time else None,
                market.status,
            ))
            self._conn.commit()

    def load_active_markets(self) -> List[MarketState]:
        """Загружает незакрытые рынки (для восстановления после рестарта).

        prob_history НЕ восстанавливается — заполняется трекером заново.
        """
        sql = "SELECT * FROM markets WHERE status NOT IN ('closed', 'skipped')"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._row_to_market(r) for r in rows]

    def update_market_status(self, condition_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE markets SET status=? WHERE condition_id=?",
                (status, condition_id)
            )
            self._conn.commit()

    # ── Positions ─────────────────────────────────────────────────────────

    def save_position(self, position: Position) -> None:
        sql = """
            INSERT OR REPLACE INTO positions
                (condition_id, outcome, amount, entry_price,
                 entry_time, simulation, order_id)
            VALUES (?,?,?,?,?,?,?)
        """
        with self._lock:
            self._conn.execute(sql, (
                position.condition_id, position.outcome,
                position.amount, position.entry_price,
                _ts(position.entry_time),
                int(position.simulation), position.order_id,
            ))
            self._conn.commit()

    def load_open_positions(self) -> List[Position]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
        return [self._row_to_position(r) for r in rows]

    def close_position(self, condition_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM positions WHERE condition_id=?", (condition_id,)
            )
            self._conn.commit()

    # ── Trades ────────────────────────────────────────────────────────────

    def save_trade(self, trade: Trade) -> None:
        sql = """
            INSERT INTO trades
                (condition_id, question, outcome, amount,
                 entry_price, entry_time, close_time, result, pnl, simulation)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """
        with self._lock:
            self._conn.execute(sql, (
                trade.condition_id, trade.question, trade.outcome,
                trade.amount, trade.entry_price,
                _ts(trade.entry_time), _ts(trade.close_time),
                trade.result, trade.pnl, int(trade.simulation),
            ))
            self._conn.commit()

    def load_trades(self, limit: int = 100) -> List[Trade]:
        sql = "SELECT * FROM trades ORDER BY close_time DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
        return [self._row_to_trade(r) for r in rows]

    # ── Balance ───────────────────────────────────────────────────────────

    def save_balance(self, balance: float, timestamp: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO balance_history (balance, timestamp) VALUES (?,?)",
                (balance, _ts(timestamp)),
            )
            self._conn.commit()

    def load_latest_balance(self) -> Optional[float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT balance FROM balance_history ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        return float(row["balance"]) if row else None

    # ── Stats ─────────────────────────────────────────────────────────────

    def compute_stats(self, simulation_mode: bool) -> StatsSnapshot:
        """Вычисляет агрегированную статистику из trades таблицы."""
        sql = """
            SELECT
                COUNT(*)                                          AS total,
                SUM(CASE WHEN result='won' THEN 1 ELSE 0 END)    AS wins,
                COALESCE(SUM(pnl), 0)                            AS total_pnl,
                COALESCE(SUM(amount), 1)                         AS total_amount
            FROM trades WHERE simulation=?
        """
        with self._lock:
            row = self._conn.execute(sql, (int(simulation_mode),)).fetchone()
            balance = self.load_latest_balance() or 0.0

        total = row["total"] or 0
        wins = row["wins"] or 0
        total_pnl = row["total_pnl"]
        total_amount = row["total_amount"] or 1.0
        return StatsSnapshot(
            total_trades=total,
            wins=wins,
            losses=total - wins,
            win_rate=wins / total if total else 0.0,
            total_pnl=total_pnl,
            roi=total_pnl / total_amount * 100,
            max_drawdown=0.0,   # TODO: вычислять по trades
            balance=balance,
            simulation_mode=simulation_mode,
        )

    # ── Schema ────────────────────────────────────────────────────────────

    def _create_markets_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                condition_id  TEXT    PRIMARY KEY,
                slug          TEXT    NOT NULL,
                question      TEXT    NOT NULL,
                series_ticker TEXT    NOT NULL,
                start_time    INTEGER NOT NULL,
                close_time    INTEGER NOT NULL,
                created_at    INTEGER NOT NULL,
                outcomes      TEXT    NOT NULL,
                token_ids     TEXT    NOT NULL,
                order_min_size REAL   NOT NULL,
                neg_risk      INTEGER NOT NULL,
                signal_fired  INTEGER DEFAULT 0,
                signal_time   INTEGER,
                status        TEXT    NOT NULL DEFAULT 'monitoring'
            )
        """)

    def _create_positions_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                condition_id  TEXT    PRIMARY KEY,
                outcome       TEXT    NOT NULL,
                amount        REAL    NOT NULL,
                entry_price   REAL    NOT NULL,
                entry_time    INTEGER NOT NULL,
                simulation    INTEGER NOT NULL,
                order_id      TEXT,
                FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
            )
        """)

    def _create_trades_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id  TEXT    NOT NULL,
                question      TEXT    NOT NULL,
                outcome       TEXT    NOT NULL,
                amount        REAL    NOT NULL,
                entry_price   REAL    NOT NULL,
                entry_time    INTEGER NOT NULL,
                close_time    INTEGER NOT NULL,
                result        TEXT    NOT NULL,
                pnl           REAL    NOT NULL,
                simulation    INTEGER NOT NULL
            )
        """)

    def _create_balance_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                balance   REAL    NOT NULL,
                timestamp INTEGER NOT NULL
            )
        """)

    # ── Row converters ────────────────────────────────────────────────────

    def _row_to_market(self, row: sqlite3.Row) -> MarketState:
        return MarketState(
            condition_id=row["condition_id"],
            slug=row["slug"],
            question=row["question"],
            series_ticker=row["series_ticker"],
            start_time=_dt(row["start_time"]),
            close_time=_dt(row["close_time"]),
            created_at=_dt(row["created_at"]),
            outcomes=json.loads(row["outcomes"]),
            token_ids=json.loads(row["token_ids"]),
            order_min_size=row["order_min_size"],
            neg_risk=bool(row["neg_risk"]),
            signal_fired=bool(row["signal_fired"]),
            signal_time=_dt(row["signal_time"]) if row["signal_time"] else None,
            status=row["status"],
        )

    def _row_to_position(self, row: sqlite3.Row) -> Position:
        return Position(
            condition_id=row["condition_id"],
            outcome=row["outcome"],
            amount=row["amount"],
            entry_price=row["entry_price"],
            entry_time=_dt(row["entry_time"]),
            simulation=bool(row["simulation"]),
            order_id=row["order_id"],
        )

    def _row_to_trade(self, row: sqlite3.Row) -> Trade:
        return Trade(
            condition_id=row["condition_id"],
            question=row["question"],
            outcome=row["outcome"],
            amount=row["amount"],
            entry_price=row["entry_price"],
            entry_time=_dt(row["entry_time"]),
            close_time=_dt(row["close_time"]),
            result=row["result"],
            pnl=row["pnl"],
            simulation=bool(row["simulation"]),
        )
