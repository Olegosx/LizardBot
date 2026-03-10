# LizardBot — Architecture (C4)

---

## Level 1 — System Context

```
┌──────────────────────────────────────────────────────────────────┐
│                           LizardBot                              │
│                                                                  │
│  ┌───────────────┐   SharedState   ┌────────────────────────┐   │
│  │  BotEngine    │◄───────────────►│      APIServer         │   │
│  │  (Thread 1)   │                 │      (Thread 2)        │   │
│  └──────┬────────┘                 └───────────┬────────────┘   │
│         │                                      │                │
│  ┌──────▼────────┐                 ┌───────────▼────────────┐   │
│  │ ConfigLoader  │                 │   Browser (Frontend)   │   │
│  │  (Thread 3)   │                 │   Bootstrap + Chart.js │   │
│  └───────────────┘                 └────────────────────────┘   │
└──────────┬───────────────────────────────────────────────────────┘
           │
     ┌─────┴──────────────────────┐
     ▼                            ▼
Gamma API                     CLOB API
(публичный, поиск рынков)     (авторизация по ключу, торговля)
```

**Режимы работы:**
- `simulation_mode: true` — ордера эмулируются, баланс = $100, CLOB credentials не нужны
- `simulation_mode: false` — реальная торговля через Polymarket CLOB API

---

## Level 2 — Containers

```
┌──────────────────────────── LizardBot Process ──────────────────────────────┐
│                                                                              │
│  Thread 1: BotEngine              Thread 2: APIServer                        │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │ - MarketScanner      │         │ - FastAPI HTTP        │                  │
│  │ - MarketTracker      │         │ - WebSocket stream    │                  │
│  │ - LizardStrategy     │         │ - Session Auth        │                  │
│  │ - OrderManager       │         └──────────┬───────────┘                  │
│  └──────────┬───────────┘                    │                              │
│             │                                │                              │
│             └────────────────┬───────────────┘                              │
│                              │                                              │
│                   ┌──────────▼──────────┐                                   │
│                   │     SharedState     │◄── Thread 3: ConfigLoader          │
│                   │ - config (BotConfig)│    Читает config.json              │
│                   │ - markets           │    каждые N секунд                 │
│                   │ - positions         │    mtime-based hot-reload          │
│                   │ - history           │                                    │
│                   │ - log_buffer        │                                    │
│                   │ - balance           │                                    │
│                   └──────────┬──────────┘                                   │
│                              │                                              │
│                   ┌──────────▼──────────┐                                   │
│                   │    DBRepository     │                                    │
│                   │  data/lizardbot.db  │                                    │
│                   │  (SQLite)           │                                    │
│                   └─────────────────────┘                                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Level 3 — Components

### Thread 1: BotEngine

```
BotEngine.run()  ── main loop (каждые POLL_INTERVAL=60 сек)
    │
    ├── config = shared.get_config_snapshot()   ← атомарная копия конфига
    ├── shared.set_running(config.active)
    │
    └── если config.active:
            │
            ├── _handle_commands()              ← обрабатывает служебные команды
            │
            ├── _scan_new_markets(config)
            │       └── MarketScanner.scan()
            │               └── GammaClient: GET /series?slug=...
            │                               GET /markets?slug=... (для каждого event)
            │
            ├── MarketTracker.poll_all(config)  ← обновляет prob_history
            │       └── CLOB: GET /midpoint?token_id=...  (для каждого рынка)
            │
            ├── _process_market() для каждого рынка со статусом "monitoring"
            │       ├── LizardStrategy.check_signal()
            │       ├── если нет истории → _apply_recovery_action()
            │       └── если should_trade → OrderManager.place_bet()
            │
            ├── _settle_market() для каждого закрытого рынка
            │       └── GammaClient.get_market_result() → OrderManager.settle()
            │
            └── _update_balance(config)
                    ├── simulation_mode=True → $100 один раз (не обращается к CLOB)
                    └── simulation_mode=False → CLOB get_balance_allowance()
```

### Thread 2: APIServer

```
FastAPI (uvicorn, HTTPS если ssl_certfile/ssl_keyfile заданы)
    │
    ├── GET/POST /login     ─── публичный (форма входа, bcrypt проверка)
    ├── GET      /logout    ─── публичный (удаляет cookie lz_session → 302 /login)
    │
    ├── /api/*              ─── SessionAuth (Cookie "lz_session" → username | 401)
    │   ├── GET  /api/status, /api/markets, /api/positions
    │   ├── GET  /api/history, /api/stats, /api/logs, /api/whoami
    │   ├── POST /api/start, /api/stop  → loader.patch({"active": ...})
    │   └── GET/POST/PATCH /api/config  → loader.save_full / loader.patch
    │
    ├── WS /ws              ─── cookie проверяется при handshake (close 1008 без сессии)
    │       └── WebSocketManager: snapshot при подключении + push каждые 3 сек
    │
    ├── /js, /css           ─── StaticFiles (публичные, только статика)
    │
    └── GET /, /{path}      ─── проверка cookie → 302 /login или index.html (SPA)
```

### Thread 3: ConfigLoader

```
ConfigLoader.run()  ── бесконечный цикл (каждые config_reload_interval сек)
    │
    ├── os.path.getmtime(config.json) != _last_mtime?
    │       └── да → load() → shared.update_config(new_config)
    │
    └── patch(updates) / save_full(data)
            └── вызывается из routes.py (start/stop/config save)
                атомарная запись: tmpfile + os.replace
```

---

## Level 4 — Classes & Methods

### `src/shared/models.py`

```python
@dataclass
class ProbPoint:
    timestamp: datetime
    probability: float          # 0.0 – 1.0

@dataclass
class MarketState:
    condition_id: str           # conditionId — ключ для CLOB API
    slug: str                   # URL-slug для Gamma API
    question: str
    series_ticker: str          # e.g. "btc-up-or-down-4h"
    start_time: datetime        # начало ценового окна (eventStartTime)
    close_time: datetime
    created_at: datetime
    outcomes: List[str]         # e.g. ["Up", "Down"]
    token_ids: Dict[str, str]   # {"Up": "token_id_1", "Down": "token_id_2"}
    order_min_size: float       # минимальная ставка (USDC)
    neg_risk: bool
    prob_history: List[ProbPoint] = []   # только в памяти, в БД не хранится
    signal_fired: bool = False
    signal_time: Optional[datetime] = None
    status: str = "monitoring"  # monitoring | bet_placed | closed | skipped

@dataclass
class Position:
    condition_id: str
    outcome: str
    amount: float               # USDC
    entry_price: float          # вероятность при входе (0.0–1.0)
    entry_time: datetime
    simulation: bool
    order_id: Optional[str] = None

@dataclass
class Trade:
    condition_id: str
    question: str
    outcome: str
    amount: float
    entry_price: float
    entry_time: datetime
    close_time: datetime
    result: str                 # "won" | "lost"
    pnl: float
    simulation: bool

@dataclass
class LogEntry:
    timestamp: datetime
    level: str                  # DEBUG | INFO | WARNING | ERROR
    thread: str
    message: str

@dataclass
class BotStatus:
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
class StatsSnapshot:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    roi: float
    max_drawdown: float         # TODO: не вычисляется, всегда 0.0
    balance: float
    simulation_mode: bool

@dataclass
class SignalResult:
    should_trade: bool
    outcome: Optional[str]
    probability: Optional[float]
    volatility: Optional[float]
    reason: str
    is_danger_zone: bool = False  # для OrderManager: режим 'reduce'
```

---

### `src/shared/state.py`

```python
class SharedState:
    # Всё защищено _lock: threading.RLock
    _config: BotConfig
    _markets: Dict[str, MarketState]    # condition_id → MarketState
    _positions: Dict[str, Position]     # condition_id → Position
    _history: List[Trade]
    _log_buffer: deque[LogEntry]        # последние 500 записей
    _balance: float
    _status: BotStatus

    # Конфиг
    def get_config_snapshot(self) -> BotConfig   # копия — использовать внутри одной итерации
    def update_config(self, config: BotConfig) -> None

    # Рынки
    def add_market(self, market: MarketState) -> None
    def get_market(self, condition_id: str) -> Optional[MarketState]
    def get_all_markets(self) -> List[MarketState]
    def get_monitored_condition_ids(self) -> List[str]
    def update_market(self, market: MarketState) -> None
    def append_prob_point(self, condition_id: str, probability: float) -> None
    def set_market_status(self, condition_id: str, status: str) -> None

    # Позиции
    def add_position(self, position: Position) -> None
    def get_position(self, condition_id: str) -> Optional[Position]
    def get_all_positions(self) -> List[Position]
    def settle_position(self, condition_id: str, trade: Trade) -> None
        # Удаляет из positions, добавляет в history

    # Логи
    def add_log(self, entry: LogEntry) -> None
    def get_logs(self, limit: int = 200) -> List[LogEntry]

    # Статус и баланс
    def set_running(self, running: bool) -> None
    def get_status(self) -> BotStatus
    def get_balance(self) -> float
    def update_balance(self, balance: float) -> None
    def get_stats(self) -> StatsSnapshot    # вычисляет из history
```

---

### `src/config/models.py`

```python
@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    ssl_certfile: str = ""      # путь к PEM-сертификату
    ssl_keyfile: str = ""       # путь к приватному ключу

@dataclass
class MarketFilterConfig:
    name: str                   # e.g. "BTC 4h"
    series_ticker: str          # e.g. "btc-up-or-down-4h"
    enabled: bool = True

@dataclass
class BotConfig:
    # Polymarket auth
    private_key: str            # приватный ключ Polygon-кошелька (пусто в sim mode)
    api_key: str                # CLOB API key (пусто → деривируется из private_key)
    api_secret: str
    api_passphrase: str
    funder_address: str         # адрес кошелька на Polygon (пусто в sim mode)

    # Режим
    active: bool                # True = стратегия работает
    simulation_mode: bool       # True = без реальных ордеров, баланс $100

    # Стратегия (Hypothesis 1)
    vol_threshold: float        # порог std dev; default: 0.20
    lookback_minutes: int       # окно наблюдения; default: 30
    danger_zone_action: str     # "skip" | "reduce" | "trade"
    danger_zone_reduce_factor: float  # default: 0.5 (при "reduce")
    recovery_action: str        # "skip" | "enter" | "enter_if_safe"

    # Размер ставки
    bet_mode: str               # "fixed" | "percent" | "double_on_double"
    bet_amount: float           # USDC (для "fixed")
    bet_percent: float          # % от баланса (для "percent" и "double_on_double")

    # Система
    market_filters: List[MarketFilterConfig]
    config_reload_interval: int # default: 10 (сек)
    log_level: str              # default: "INFO"
    server: ServerConfig
    users: Dict[str, str]       # username → bcrypt_hash
```

---

### `src/config/loader.py`

```python
class ConfigLoader:
    def load(self) -> BotConfig
        # Читает и парсит config.json → BotConfig

    def save_full(self, data: dict) -> None
        # Атомарная запись: tmpfile → os.replace → config.json

    def patch(self, updates: dict) -> None
        # Читает конфиг, мерджит updates, вызывает save_full
        # Используется из routes.py: start/stop, sim-toggle, частичные изменения

    def run(self) -> None
        # Thread 3: while True → if mtime изменился → update_config → sleep(interval)
```

---

### `src/client/polymarket.py`

```python
class GammaClient:
    """Публичный API gamma-api.polymarket.com, авторизация не нужна."""

    def get_markets_by_series(self, series_ticker: str) -> List[dict]
        # 1. GET /series?slug={series_ticker}  → список open events (тикеры)
        # 2. GET /markets?slug={event_slug}     → данные рынка для каждого события
        # ВАЖНО: параметр series_slug в /markets Gamma API игнорирует — нерабочий

    def get_market_by_slug(self, slug: str) -> Optional[dict]
        # GET /markets?slug={slug}

class PolymarketClient:
    _gamma: GammaClient
    _clob: ClobClient            # py_clob_client

    # В simulation_mode: ClobClient без credentials (только публичные эндпоинты)
    # В real mode: ClobClient с private_key, POLYGON chain_id, funder

    def get_active_markets(self, series_ticker: str) -> List[dict]
    def get_market_by_slug(self, slug: str) -> Optional[dict]
    def get_market_result(self, slug: str) -> Optional[str]
        # outcome у которого outcomePrices == "1.0" → победитель

    def get_market_probability(self, token_id: str) -> Optional[float]
        # CLOB GET /midpoint?token_id=... → float (None при ошибке)
    def get_balance(self) -> float
        # CLOB get_balance_allowance(AssetType.COLLATERAL) → USDC
    def place_order(self, token_id, amount, neg_risk, order_min_size) -> dict
        # Проверяет amount >= order_min_size
        # FOK market order через CLOB API

# Исключения:
# PolymarketError → базовое
# PolymarketNetworkError → сетевая ошибка
# PolymarketOrderError → ошибка ордера
```

**Маппинг outcomes → token_ids:**
`clobTokenIds[i]` ↔ `outcomes[i]` (1:1 по индексу).
В `MarketState.token_ids`: `{"Up": "token_id_1", "Down": "token_id_2"}`.

---

### `src/bot/strategy.py`

```python
class LizardStrategy:
    """Hypothesis 1: ставим в T-30 при низкой волатильности."""

    def check_signal(self, market: MarketState, config: BotConfig) -> SignalResult
        # 1. signal_fired == True           → False (один сигнал на рынок)
        # 2. minutes_to_close > lookback    → False (слишком рано)
        # 3. volatility is None             → False (нет истории)
        # 4. volatility > vol_threshold     → False
        # 5. get_leading_outcome(prob_first, outcomes) → (name, prob)
        # 6. is_danger_zone(prob)?          → _apply_danger_zone_action()
        # 7. иначе                          → SignalResult(True, ...)

    def get_leading_outcome(self, prob_first: float, outcomes: List[str]) -> Tuple[str, float]
        # prob_first = P(outcomes[0]); если >= 0.5 → (outcomes[0], prob_first)
        # иначе → (outcomes[1], 1 - prob_first)

    def calculate_volatility(self, history: List[ProbPoint], window_minutes: int) -> Optional[float]
        # statistics.stdev за последние window_minutes; None если < 2 точек

    def is_danger_zone(self, prob: float) -> bool
        # 0.80 <= prob <= 0.90

    def _apply_danger_zone_action(self, ..., config) -> SignalResult
        # "skip"   → SignalResult(False)
        # "reduce" → SignalResult(True, is_danger_zone=True)   → OrderManager уменьшит ставку
        # "trade"  → SignalResult(True, is_danger_zone=True)
```

---

### `src/bot/scanner.py`

```python
class MarketScanner:
    _client: PolymarketClient
    _tracked: Dict[str, bool]   # condition_id → True (локальный кэш дубликатов)

    def scan(self, config: BotConfig) -> List[MarketState]
        # Итерирует по enabled market_filters → _scan_series() для каждого

    def mark_tracked(self, condition_id: str) -> None
        # Вызывается из BotEngine после добавления рынка в SharedState

    def _scan_series(self, market_filter: MarketFilterConfig) -> List[MarketState]
    def _is_tradeable(self, raw: dict) -> bool
        # enableOrderBook == True и archived == False
    def _to_market_state(self, raw: dict, series_ticker: str) -> Optional[MarketState]
        # token_ids = dict(zip(outcomes, clobTokenIds))
```

---

### `src/bot/tracker.py`

```python
class MarketTracker:
    _client: PolymarketClient
    _shared: SharedState

    def poll_all(self, config: BotConfig) -> None
        # Для всех рынков в статусе monitoring/bet_placed:
        # get_market_probability(token_ids[outcomes[0]]) → append_prob_point()

    def get_closed_ids(self) -> List[str]
        # Возвращает condition_id рынков у которых close_time <= now
```

---

### `src/bot/order_manager.py`

```python
_TAKER_FEE_RATE = 0.01   # 1% комиссия

class OrderManager:
    _client: PolymarketClient
    _shared: SharedState
    _db: DBRepository
    _initial_balance: float     # для режима double_on_double

    def set_initial_balance(self, balance: float) -> None

    def calculate_bet_amount(self, config: BotConfig, signal: SignalResult) -> float
        # "fixed"            → config.bet_amount
        # "percent"          → balance * bet_percent / 100
        # "double_on_double" → bet_amount * floor(balance / initial_balance)
        # Если signal.is_danger_zone и danger_zone_action="reduce":
        #     amount *= danger_zone_reduce_factor

    def place_bet(self, market: MarketState, signal: SignalResult, config: BotConfig) -> None
        # simulation=True  → Position без реального ордера
        # simulation=False → client.place_order() → Position с order_id
        # Сохраняет: SharedState.add_position() + DBRepository.save_position()
        # Устанавливает market.signal_fired=True, market.status="bet_placed"

    def settle(self, condition_id: str, result: str, config: BotConfig) -> None
        # result = победивший исход (e.g. "Up")
        # PnL: won  → (1 / entry_price - 1) * amount - TAKER_FEE_RATE * amount
        #      lost → -amount
        # Создаёт Trade, вызывает SharedState.settle_position() + DBRepository.save_trade()
```

---

### `src/bot/engine.py`

```python
class BotEngine:
    POLL_INTERVAL: int = 60     # секунд между итерациями

    def run(self) -> None
        # Поток живёт всегда (daemon=True)
        # Если config.active == False — ждёт, не торгует

    def stop(self) -> None       # Устанавливает _stop_event (SIGTERM/SIGINT)

    def _tick(self, config: BotConfig) -> None
        # handle_commands → scan → poll → process → settle → update_balance

    def _process_market(self, market: MarketState, config: BotConfig) -> None
        # check_signal() → нет истории → _apply_recovery_action()
        # should_trade → order_manager.place_bet()

    def _apply_recovery_action(self, market, config) -> Optional[SignalResult]
        # Для рынков у которых окно T-30 уже прошло (бот был офлайн)
        # "skip"           → set_market_status("skipped"), None
        # "enter"          → SignalResult(True, ...) — входим без проверки
        # "enter_if_safe"  → SignalResult(True, ...) только если prob вне danger_zone

    def _settle_market(self, condition_id: str, config: BotConfig) -> None
        # get_market_result(slug) → order_manager.settle()

    def _recover_state(self) -> None
        # Вызывается до старта потоков
        # Загружает из DB: активные рынки + открытые позиции → SharedState
        # Помечает рынки как tracked в MarketScanner
        # Для закрытых рынков с открытой позицией: settle немедленно

    def _update_balance(self, config: BotConfig) -> None
        # simulation_mode=True  → $100 один раз (не обращается к CLOB)
        # simulation_mode=False → client.get_balance() → shared + db
```

---

### `src/db/repository.py`

```python
class DBRepository:
    """SQLite, потокобезопасность через threading.Lock."""

    def init_schema(self) -> None

    # Markets
    def save_market(self, market: MarketState) -> None      # INSERT OR REPLACE
    def load_active_markets(self) -> List[MarketState]      # status NOT IN ('closed','skipped')
    def update_market_status(self, condition_id: str, status: str) -> None

    # Positions
    def save_position(self, position: Position) -> None     # INSERT OR REPLACE
    def load_open_positions(self) -> List[Position]
    def close_position(self, condition_id: str) -> None     # DELETE

    # Trades
    def save_trade(self, trade: Trade) -> None
    def load_trades(self, limit: int = 100) -> List[Trade]

    # Balance
    def save_balance(self, balance: float, timestamp: datetime) -> None
    def compute_stats(self, simulation_mode: bool) -> StatsSnapshot
```

---

### `src/api/auth.py`

```python
SESSION_COOKIE = "lz_session"
_STORE: dict = {}           # token → username (in-memory, сбрасывается при рестарте)

def create_session(username: str) -> str
    # secrets.token_urlsafe(32) → _STORE[token] = username → token

def delete_session(token: str) -> None
def get_session_user(token: str) -> Optional[str]

def check_credentials(username, password, shared) -> bool
    # config.users[username] → bcrypt.checkpw(password, hash)

class SessionAuth:
    # FastAPI Dependency: Cookie("lz_session") → username | HTTP 401
    def __call__(self, lz_session: Optional[str] = Cookie(default=None)) -> str
```

**Важно:** `from __future__ import annotations` **не используется** в `routes.py`.
Причина: локальный тип `Username = Annotated[str, Depends(auth)]` становится строкой при отложенных аннотациях, `get_type_hints()` не находит его в глобальном namespace, FastAPI трактует `username` как query-параметр и возвращает 422.

---

### `src/api/websocket.py`

```python
_HISTORY_IN_MARKETS = 60    # последних точек prob_history в payload рынка

class WebSocketManager:
    _connections: Set[WebSocket]
    _log_cursor: int            # индекс последнего отправленного лога

    async def connect(self, ws: WebSocket) -> None
        # accept() → _push_snapshot(ws)

    async def push_loop(self) -> None
        # asyncio task, каждые 3 сек: status + markets + logs_append

# При подключении (snapshot):
#   status, stats, markets, positions, history (50), logs (100)

# Периодически (каждые 3 сек):
#   status, markets, logs_append (только новые, по _log_cursor)

# market_summary payload:
# {condition_id, slug, question, series_ticker, close_time, start_time,
#  status, signal_fired, outcomes, latest_prob, latest_prob_ts,
#  prob_history: последние 60 точек [{timestamp, probability}]}
```

---

### `src/api/server.py`

```python
class APIServer:
    def run(self) -> None
        # Запускает uvicorn
        # SSL: если ssl_certfile и ssl_keyfile заданы → HTTPS
        # Если SSL не задан → WARNING в лог, запуск без TLS

# _FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"  (абсолютный путь)

# Порядок регистрации маршрутов (важен для catch-all):
# 1. GET  /login      ← публичный, /login?error=1 при неверных данных
# 2. POST /login      ← проверка credentials → set cookie → 303 /
# 3. GET  /logout     ← удалить cookie → 302 /login
# 4. /api/*           ← SessionAuth (401 без сессии)
# 5. WS  /ws          ← cookie при handshake → close(1008) без сессии
# 6. /js, /css        ← StaticFiles (публичные)
# 7. GET  /           ← cookie → 302 /login или index.html
# 8. GET  /{path}     ← cookie → 302 /login или index.html (SPA fallback)
```

---

### `src/main.py`

```python
def setup_logging(level, shared):
    # fmt: "%(asctime)s [%(levelname)-8s] [%(threadName)s] %(name)s: %(message)s"
    # Handlers: StreamHandler(stdout) + FileHandler("logs/lizardbot.log") + SharedLogHandler
    # SharedLogHandler → каждый log-вызов → LogEntry → shared.add_log() → фронтенд

def main():
    db = DBRepository("data/lizardbot.db"); db.init_schema()
    config = ConfigLoader("config.json").load()
    shared = SharedState(config=config)
    setup_logging(config.log_level, shared)

    client     = PolymarketClient(config)
    engine     = BotEngine(client=client, shared=shared, db=db)
    cfg_loader = ConfigLoader("config.json", shared=shared)
    api        = APIServer(shared=shared, loader=cfg_loader)

    engine._recover_state()     # восстановление из DB до старта потоков

    Thread(target=engine.run,     name="BotEngine",    daemon=True).start()
    Thread(target=api.run,        name="APIServer",    daemon=True).start()
    Thread(target=cfg_loader.run, name="ConfigLoader", daemon=True).start()

    # SIGINT/SIGTERM → engine.stop() → join
```

---

## Database Schema

```sql
CREATE TABLE markets (
    condition_id    TEXT    PRIMARY KEY,
    slug            TEXT    NOT NULL,
    question        TEXT    NOT NULL,
    series_ticker   TEXT    NOT NULL,
    start_time      TEXT    NOT NULL,   -- ISO datetime
    close_time      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    outcomes        TEXT    NOT NULL,   -- JSON array: ["Up","Down"]
    token_ids       TEXT    NOT NULL,   -- JSON object: {"Up":"0x...","Down":"0x..."}
    order_min_size  REAL    NOT NULL,
    neg_risk        INTEGER NOT NULL,   -- 0|1
    signal_fired    INTEGER DEFAULT 0,
    signal_time     TEXT,
    status          TEXT    NOT NULL    -- monitoring|bet_placed|closed|skipped
);
-- prob_history НЕ хранится в БД — только в памяти (SharedState.markets[*].prob_history)

CREATE TABLE positions (
    condition_id    TEXT    PRIMARY KEY,
    outcome         TEXT    NOT NULL,
    amount          REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    simulation      INTEGER NOT NULL,   -- 0|1
    order_id        TEXT
);

CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    condition_id    TEXT    NOT NULL,
    question        TEXT    NOT NULL,
    outcome         TEXT    NOT NULL,
    amount          REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_time      TEXT    NOT NULL,
    close_time      TEXT    NOT NULL,
    result          TEXT    NOT NULL,   -- won|lost
    pnl             REAL    NOT NULL,
    simulation      INTEGER NOT NULL    -- 0|1
);

CREATE TABLE balance_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    balance         REAL    NOT NULL,
    timestamp       TEXT    NOT NULL
);
```

---

## File Structure

```
LizardBot/
├── install.sh                  # автоматический инсталлятор Ubuntu (Python 3.11)
├── config.json                 # ! в .gitignore (содержит приватные ключи)
├── config.example.json         # шаблон без секретов
├── requirements.txt
├── ARCHITECTURE.md
├── DEPLOY.md
├── CLAUDE.md
├── src/
│   ├── main.py                 # точка входа: 3 потока + setup_logging + SIGTERM
│   ├── shared/
│   │   ├── models.py           # все dataclass-модели (MarketState, Position, Trade, ...)
│   │   ├── state.py            # SharedState — единственный канал между потоками
│   │   └── log_handler.py      # SharedLogHandler → logging → фронтенд в реальном времени
│   ├── config/
│   │   ├── models.py           # BotConfig, ServerConfig, MarketFilterConfig
│   │   └── loader.py           # ConfigLoader: mtime-based hot-reload + atomic save
│   ├── client/
│   │   └── polymarket.py       # GammaClient (/series→/markets) + PolymarketClient (CLOB)
│   ├── bot/
│   │   ├── engine.py           # BotEngine (Thread 1): главный цикл оркестратор
│   │   ├── scanner.py          # MarketScanner: поиск новых рынков через Gamma API
│   │   ├── tracker.py          # MarketTracker: обновление prob_history + детектирование закрытий
│   │   ├── strategy.py         # LizardStrategy: Hypothesis 1 (T-30, vol filter, danger zone)
│   │   └── order_manager.py    # OrderManager: ставки, расчёт PnL, закрытие позиций
│   ├── db/
│   │   └── repository.py       # DBRepository: SQLite (markets, positions, trades, balance)
│   └── api/
│       ├── server.py           # FastAPI app + uvicorn (HTTPS) + маршрутизация
│       ├── routes.py           # /api/* эндпоинты (SessionAuth)
│       ├── websocket.py        # WebSocketManager: snapshot + periodic push каждые 3 сек
│       └── auth.py             # SessionAuth: cookie lz_session, bcrypt, in-memory _STORE
├── frontend/
│   ├── login.html              # страница входа (<form method="post" action="/login">)
│   ├── index.html              # SPA: вкладки Мониторинг / Логи / Настройки
│   ├── js/app.js               # WS-клиент, рендер карточек, Chart.js, редактор конфига
│   └── css/style.css           # CSS vars --lz-* (dark/:root, light/[data-bs-theme="light"])
├── tests/
│   ├── test_strategy.py
│   └── test_order_manager.py
├── data/
│   └── lizardbot.db            # SQLite (создаётся автоматически)
├── logs/
│   └── lizardbot.log           # ротация через logrotate (14 дней, сжатие)
└── certs/
    ├── cert.pem                # ! в .gitignore
    └── key.pem                 # ! в .gitignore
```

---

## Frontend Architecture

```
frontend/login.html
  └── <form method="post" action="/login">
        username + password → POST /login → cookie lz_session → 303 /

frontend/index.html (SPA)
  ├── Navbar: статус-бейдж, баланс, WS-иконка, username, /logout, тема
  ├── Simulation banner (если simulation_mode=True)
  ├── Control panel: Start/Stop, чекбокс Simulation, P&L / Trades / WinRate
  └── Вкладки (Bootstrap Tabs):
      ├── Мониторинг:
      │   ├── Рынки (market cards с Chart.js)
      │   ├── Открытые позиции (таблица)
      │   └── История сделок (таблица)
      ├── Логи (терминал, real-time)
      └── Настройки (редактор конфига)

frontend/js/app.js
  ├── WebSocket: connectWS() → автопереподключение каждые 3 сек
  ├── handleEvent(): status | stats | markets | positions | history | logs | logs_append
  ├── renderMarkets():
  │   ├── Сортировка: активные (opened) выше pending (ещё не открытых)
  │   ├── Pending-карточки: свёрнуты, показывают "Ожидаем открытия + через N мин"
  │   └── Chart.js line chart:
  │       ├── Тип: "time" (x: Date, y: число от 0 до 100)
  │       ├── Диапазон X: start_time → close_time (фиксированный)
  │       ├── Единица: "minute" (≤2ч) или "hour" (>2ч)
  │       └── Адаптер: chartjs-adapter-date-fns
  ├── apiFetch(): при 401 → location.href='/login'
  └── Тема: localStorage "lz-theme", dark/light через data-bs-theme
```

---

## Key Design Decisions

**1. SharedState — единственный канал между потоками**
Потоки не общаются напрямую. Все данные идут через SharedState (RLock). DBRepository — отдельный слой под SharedState с собственным Lock.

**2. prob_history только в памяти**
Не хранится в БД — заполняется трекером заново после каждого рестарта. Экономит место и упрощает схему.

**3. Hot-reload конфига без перезапуска**
ConfigLoader (Thread 3) следит за mtime. BotEngine и APIServer копируют конфиг в начале каждой итерации через `get_config_snapshot()` — изменения вступают в силу максимум через POLL_INTERVAL сек.

**4. from __future__ import annotations запрещён в routes.py**
FastAPI использует `get_type_hints()` для dependency injection. Отложенные аннотации превращают локальный тип `Username` в строку, которую FastAPI не может разрешить → трактует как query-параметр → 422.

**5. Gamma API: /series endpoint вместо /markets?series_slug=...**
Параметр `series_slug` в `/markets` игнорируется Gamma API. Правильный путь: `/series?slug=TICKER` → event tickers → `/markets?slug=TICKER` для каждого.

**6. Python 3.11 обязателен**
Python 3.12.x имеет известные проблемы с segfault при комбинации C-расширений (cytoolz, ckzg) из стека py-clob-client с модулем `logging`. install.sh автоматически выбирает Python 3.11.
