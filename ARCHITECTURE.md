# LizardBot — Architecture (C4)

---

## Level 1 — System Context

```
┌─────────────────────────────────────────────────────────────┐
│                        LizardBot                            │
│                                                             │
│  ┌──────────────┐   SharedState   ┌──────────────────────┐ │
│  │  BotEngine   │◄───────────────►│    APIServer         │ │
│  │  (Thread 1)  │                 │    (Thread 2)        │ │
│  └──────┬───────┘                 └──────────┬───────────┘ │
│         │                                    │             │
│  ┌──────▼───────┐                 ┌──────────▼───────────┐ │
│  │ConfigLoader  │                 │  Frontend (Browser)  │ │
│  │  (Thread 3)  │                 │  Bootstrap+Chart.js  │ │
│  └──────────────┘                 └──────────────────────┘ │
└──────────┬──────────────────────────────────────────────────┘
           │
           ▼
  ┌─────────────────┐
  │  Polymarket API │
  │  (CLOB API)     │
  └─────────────────┘
```

**Режимы работы:**
- `simulation_mode: true` — сделки эмулируются, реальных ордеров нет. Фронтенд показывает баннер **[SIMULATION]**
- `simulation_mode: false` — реальная торговля через Polymarket CLOB API

---

## Level 2 — Containers

```
┌─────────────────────── LizardBot Process ──────────────────────────┐
│                                                                     │
│  Thread 1: BotEngine          Thread 2: APIServer                  │
│  ┌─────────────────────┐      ┌─────────────────────┐              │
│  │ - MarketScanner     │      │ - FastAPI HTTP       │              │
│  │ - MarketTracker     │      │ - WebSocket stream   │              │
│  │ - Strategy          │      │ - Basic Auth         │              │
│  │ - OrderManager      │      └──────────┬──────────┘              │
│  └──────────┬──────────┘                 │                         │
│             │                            │                         │
│             └──────────┬─────────────────┘                         │
│                        │                                           │
│              ┌─────────▼──────────┐                                │
│              │    SharedState      │   Thread 3: ConfigLoader       │
│              │ - config            │◄──────────────────────────────│
│              │ - markets           │   Читает config.json           │
│              │ - positions         │   каждые N секунд              │
│              │ - history           │                                │
│              │ - log_buffer        │                                │
│              │ - commands          │                                │
│              │ - balance           │                                │
│              └─────────┬──────────┘                                │
│                        │                                           │
│              ┌─────────▼──────────┐                                │
│              │   DBRepository      │                                │
│              │   (lizardbot.db)    │                                │
│              └────────────────────┘                                │
└─────────────────────────────────────────────────────────────────────┘
           │                         │
           ▼                         ▼
  Polymarket CLOB API          Browser (Frontend)
```

---

## Level 3 — Components

### Thread 1: BotEngine

```
BotEngine.run()  ──── main loop (every POLL_INTERVAL seconds)
    │
    ├── _handle_commands()         ← читает из SharedState.commands
    │
    ├── ConfigLoader sync          ← копирует config из SharedState перед итерацией
    │
    ├── MarketScanner.scan()       ← находит новые BTC Up/Down рынки
    │       │
    │       └── PolymarketClient.get_btc_markets()
    │
    ├── MarketTracker.poll_all()   ← обновляет prob_history всех рынков
    │       │
    │       └── PolymarketClient.get_market_probability(market_id)
    │
    ├── _process_market()          ← для каждого отслеживаемого рынка
    │       │
    │       ├── check if T-30 window reached
    │       ├── Strategy.check_signal()
    │       └── OrderManager.place_bet()  (если сигнал)
    │
    ├── MarketTracker.check_closed_markets()
    │       │
    │       └── OrderManager.settle()  ← при закрытии рынка
    │
    └── PolymarketClient.get_balance() → SharedState.update_balance()
```

### Thread 2: APIServer

```
FastAPI app
    │
    ├── Middleware: BasicAuthMiddleware  ← проверяет users из config
    │
    ├── GET  /api/status     → SharedState.status
    ├── GET  /api/markets    → SharedState.markets
    ├── GET  /api/positions  → SharedState.positions
    ├── GET  /api/history    → DBRepository.load_trades()
    ├── GET  /api/stats      → DBRepository.compute_stats()
    ├── GET  /api/logs       → SharedState.log_buffer
    ├── POST /api/start      → loader.patch({"active": True})
    ├── POST /api/stop       → loader.patch({"active": False})
    ├── GET  /api/config     → текущий BotConfig (для редактора)
    ├── POST /api/config     → сохранить полный конфиг
    ├── PATCH /api/config    → частичный апдейт (simulation_mode, active и т.д.)
    └── WS   /ws             → WebSocketManager
                                    │
                                    └── pushes events from SharedState
```

### Thread 3: ConfigLoader

```
ConfigLoader.run()  ──── бесконечный цикл (каждые config_reload_interval сек)
    │
    ├── _has_changed()     ← сравнивает mtime файла
    ├── load()             ← парсит config.json → BotConfig
    └── SharedState.update_config(config)
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
    start_time: datetime        # Начало ценового окна (eventStartTime)
    close_time: datetime
    created_at: datetime
    outcomes: List[str]         # e.g. ["Up", "Down"]
    token_ids: Dict[str, str]   # outcome -> clobTokenId
    order_min_size: float       # Минимальный размер ставки (USDC), поле рынка
    neg_risk: bool
    prob_history: List[ProbPoint]
    signal_fired: bool
    signal_time: Optional[datetime]
    status: str                 # 'monitoring'|'bet_placed'|'closed'|'skipped'

@dataclass
class Position:
    market_id: str
    outcome: str
    amount: float
    entry_price: float
    entry_time: datetime
    simulation: bool
    order_id: Optional[str]

@dataclass
class Trade:
    market_id: str
    question: str
    outcome: str
    amount: float
    entry_price: float
    entry_time: datetime
    close_time: datetime
    result: str                 # 'won'|'lost'
    pnl: float
    simulation: bool

@dataclass
class LogEntry:
    timestamp: datetime
    level: str                  # DEBUG|INFO|WARNING|ERROR
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
class Command:
    action: str                 # 'start'|'stop'|'reload_config'
    params: Dict[str, Any]

@dataclass
class StatsSnapshot:
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
    should_trade: bool
    outcome: Optional[str]
    probability: Optional[float]
    volatility: Optional[float]
    reason: str
    is_danger_zone: bool = False    # для OrderManager.calculate_bet_amount (режим 'reduce')
```

---

### `src/shared/state.py`

```python
class SharedState:
    # Поля (защищены _lock: threading.RLock)
    config: BotConfig
    markets: Dict[str, MarketState]
    positions: Dict[str, Position]      # market_id → Position (открытые)
    history: List[Trade]
    status: BotStatus
    log_buffer: deque[LogEntry]         # последние LOG_BUFFER_SIZE записей
    commands: Queue[Command]
    balance: float

    # Методы
    def get_config_snapshot(self) -> BotConfig
        # Потокобезопасная копия конфига (для использования внутри итерации)

    def update_config(self, config: BotConfig) -> None

    def update_market(self, market: MarketState) -> None

    def add_log(self, entry: LogEntry) -> None

    def get_logs(self, limit: int = 200) -> List[LogEntry]

    def push_command(self, cmd: Command) -> None

    def pop_command(self) -> Optional[Command]

    def update_balance(self, balance: float) -> None

    def record_position(self, position: Position) -> None

    def settle_position(self, market_id: str, trade: Trade) -> None
        # Удаляет из positions, добавляет в history

    def get_stats(self) -> StatsSnapshot
```

---

### `src/config/models.py`

```python
@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8443
    ssl_certfile: str = ""   # Путь к PEM-сертификату (относительный от CWD или абсолютный)
    ssl_keyfile: str = ""    # Путь к приватному ключу PEM

@dataclass
class MarketFilterConfig:
    name: str               # e.g. "BTC 4h"
    series_ticker: str      # e.g. "btc-up-or-down-4h"
    enabled: bool

@dataclass
class BotConfig:
    # Polymarket auth
    private_key: str        # Приватный ключ Polygon-кошелька
    api_key: str            # CLOB API key (опц.: если пусто — деривируется)
    api_secret: str
    api_passphrase: str
    funder_address: str     # Адрес кошелька на Polygon

    # Режим
    active: bool                    # true = стратегия работает (управляется через конфиг)
    simulation_mode: bool           # true = эмуляция, false = реальная торговля

    # Стратегия
    vol_threshold: float            # default: 0.20
    lookback_minutes: int           # default: 30
    danger_zone_action: str         # 'skip'|'reduce'|'trade'
    danger_zone_reduce_factor: float# default: 0.5 (при 'reduce')
    recovery_action: str            # 'skip'|'enter'|'enter_if_safe'

    # Размер ставки
    bet_mode: str                   # 'fixed'|'percent'|'double_on_double'
    bet_amount: float               # для 'fixed'
    bet_percent: float              # для 'percent' и 'double_on_double'

    # Система
    config_reload_interval: int     # default: 10 (сек)
    log_level: str                  # default: 'INFO'

    # Сервер
    server: ServerConfig

    # Пользователи (Basic Auth)
    users: Dict[str, str]           # username → bcrypt_hash
```

---

### `src/config/loader.py`

```python
class ConfigLoader:
    config_path: str
    shared: SharedState
    interval: int
    _last_mtime: float

    def load(self) -> BotConfig
        # Читает и парсит config.json → BotConfig

    def save_full(self, data: dict) -> None
        # Записывает dict как config.json атомарно (tmpfile + os.replace)

    def patch(self, updates: dict) -> None
        # Читает config.json, мерджит updates, вызывает save_full
        # Используется для старта/стопа и чекбокса симуляции

    def run(self) -> None
        # Thread 3 main loop:
        # while True:
        #   if _has_changed(): shared.update_config(load())
        #   sleep(interval)

    def _has_changed(self) -> bool
        # Сравнивает os.path.getmtime с _last_mtime
```

---

### `src/client/polymarket.py`

Два API: **Gamma** (`gamma-api.polymarket.com`, без авторизации) и **CLOB** (`clob.polymarket.com`, авторизация через приватный ключ).

```python
class GammaClient:
    # Поиск рынков и метаданные
    def get_markets_by_series(self, series_ticker: str) -> List[dict]
        # GET /series?slug={series_ticker} → берём open events
        # затем GET /markets?slug={event_slug} для каждого события
        # (параметр series_slug в /markets Gamma API игнорирует)
    def get_market_by_slug(self, slug: str) -> Optional[dict]

class PolymarketClient:
    _gamma: GammaClient
    _clob: ClobClient   # py_clob_client.client.ClobClient

    # Gamma методы (поиск рынков)
    def get_active_markets(self, series_ticker: str) -> List[dict]
    def get_market_by_slug(self, slug: str) -> Optional[dict]
    def get_market_result(self, slug: str) -> Optional[str]
        # outcome с outcomePrices == "1.0" → победитель

    # CLOB методы (котировки и ордера)
    def get_market_probability(self, token_id: str) -> Optional[float]
        # get_midpoint(token_id) → {"mid": "0.87"} → float
    def get_balance(self) -> float
        # get_balance_allowance(AssetType.COLLATERAL)
    def place_order(self, token_id: str, amount: float, neg_risk: bool, order_min_size: float) -> dict
        # Проверяет amount >= order_min_size; размещает FOK market order

class PolymarketError(Exception): ...
class PolymarketNetworkError(PolymarketError): ...
class PolymarketOrderError(PolymarketError): ...
```

**Маппинг outcomes → token_ids:** `clobTokenIds[i]` соответствует `outcomes[i]` (1:1).
В `MarketState.token_ids` хранится как `Dict[str, str]`: `{"Up": "token1", "Down": "token2"}`.

---

### `src/bot/strategy.py`

```python
class LizardStrategy:
    # Параметры стратегии берутся из BotConfig (не хранятся в классе)

    def check_signal(self, market: MarketState, config: BotConfig) -> SignalResult
        # 1. signal_fired → False
        # 2. minutes_to_close > lookback_minutes → рано
        # 3. volatility=None → нет истории
        # 4. volatility > vol_threshold → False
        # 5. get_leading_outcome → outcome, prob
        # 6. is_danger_zone → _apply_danger_zone_action

    def get_leading_outcome(self, prob_first: float, outcomes: List[str]) -> Tuple[str, float]
        # prob_first = P(outcomes[0]); возвращает (leading_name, P(leading))

    def calculate_volatility(self, history: List[ProbPoint], window_minutes: int) -> Optional[float]
        # statistics.stdev за последние window_minutes минут; None если < 2 точек

    def is_danger_zone(self, prob: float) -> bool
        # 0.80 <= prob <= 0.90

    def _apply_danger_zone_action(self, outcome, prob, volatility, config) -> SignalResult
        # skip → False | reduce → True, is_danger_zone=True | trade → True, is_danger_zone=True
```

`SignalResult.is_danger_zone: bool = False` — флаг для `OrderManager.calculate_bet_amount`.

---

### `src/bot/scanner.py`

```python
class MarketScanner:
    client: PolymarketClient
    _tracked: Dict[str, bool]   # condition_id -> True (локальный кэш)

    def scan(self, config: BotConfig) -> List[MarketState]
        # Итерирует по всем включённым market_filters из конфига
        # Возвращает новые MarketState (BotEngine добавляет в SharedState)

    def mark_tracked(self, condition_id: str) -> None
        # Вызывается из BotEngine после add_market в SharedState

    def _scan_series(self, market_filter: MarketFilterConfig) -> List[MarketState]
    def _is_tracked(self, condition_id: str) -> bool
    def _is_tradeable(self, raw: dict) -> bool
        # Проверяет enableOrderBook=True и not archived
    def _to_market_state(self, raw: dict, series_ticker: str) -> Optional[MarketState]
        # token_ids = dict(zip(outcomes, clobTokenIds))
        # Конвертирует ответ API → MarketState
```

---

### `src/bot/tracker.py`

```python
class MarketTracker:
    client: PolymarketClient
    shared: SharedState

    def poll_all(self, config: BotConfig) -> None
        # Обновляет prob_history для всех рынков в SharedState

    def poll_market(self, market_id: str) -> Optional[float]
        # Один запрос вероятности → добавляет ProbPoint в MarketState

    def check_closed_markets(self, config: BotConfig) -> List[str]
        # Проверяет рынки у которых close_time <= now
        # Возвращает список закрытых market_id
```

---

### `src/bot/order_manager.py`

```python
class OrderManager:
    client: PolymarketClient
    shared: SharedState
    db: DBRepository

    def calculate_bet_amount(self, config: BotConfig) -> float
        # 'fixed'           → config.bet_amount
        # 'percent'         → balance * config.bet_percent / 100
        # 'double_on_double'→ bet_amount * floor(balance / initial_balance)

    def place_bet(
        self,
        market: MarketState,
        signal: SignalResult,
        config: BotConfig
    ) -> None
        # simulation=True → записывает Position без реального ордера
        # simulation=False → client.place_order() → записывает Position
        # Сохраняет в DB + SharedState

    def settle(
        self,
        market_id: str,
        result: str,
        config: BotConfig
    ) -> None
        # Вычисляет PnL
        # Создаёт Trade
        # Обновляет SharedState + DB

    def _calc_pnl(
        self,
        position: Position,
        result: str
    ) -> float
        # Won: (1 / entry_price - 1) * amount - commission
        # Lost: -amount
```

---

### `src/bot/engine.py`

```python
class BotEngine:
    _client: PolymarketClient
    _shared: SharedState
    _db: DBRepository
    _strategy: LizardStrategy
    _scanner: MarketScanner
    _tracker: MarketTracker
    _order_manager: OrderManager
    _stop_event: threading.Event

    POLL_INTERVAL: int = 60   # секунд между итерациями

    def stop(self) -> None                      # Устанавливает _stop_event (SIGTERM)

    def run(self) -> None
        # Thread 1 main loop. Поток живёт всегда.
        # Каждую итерацию: shared.set_running(config.active)
        # Торговая логика (_tick) выполняется только если config.active == True

    def _tick(self, config: BotConfig) -> None
        # handle_commands → scan → poll → process each market → settle closed → update balance
    def _scan_new_markets(self, config: BotConfig) -> None

    def _process_market(self, market: MarketState, config: BotConfig) -> None
        # check_signal → если нет истории → _apply_recovery_action
        # если should_trade → order_manager.place_bet()

    def _apply_recovery_action(self, market: MarketState, config: BotConfig) -> Optional[SignalResult]
        # skip → set status=skipped
        # enter → SignalResult(True, ...)
        # enter_if_safe → только если prob вне danger_zone

    def _settle_market(self, condition_id: str, config: BotConfig) -> None
        # get_market_result(slug) → order_manager.settle()

    def _recover_state(self) -> None            # Загружает рынки и позиции из DB
    def _recover_closed_markets(self, position_ids: set) -> None
    def _handle_commands(self) -> None          # служебные команды (stop через SIGTERM, не через фронт)
    def _update_balance(self, config: BotConfig) -> None
        # simulation_mode=True → не обращается к CLOB, устанавливает $100 один раз (при balance=0)
        # simulation_mode=False → get_balance() → shared + db
```

---

### `src/db/repository.py`

```python
class DBRepository:
    db_path: str
    _conn: sqlite3.Connection

    def init_schema(self) -> None
        # Создаёт таблицы если не существуют

    # Markets
    def save_market(self, market: MarketState) -> None
    def load_active_markets(self) -> List[MarketState]
    def update_market_status(self, market_id: str, status: str) -> None
    def save_prob_point(self, market_id: str, point: ProbPoint) -> None

    # Positions
    def save_position(self, position: Position) -> None
    def load_open_positions(self) -> List[Position]
    def close_position(self, market_id: str) -> None

    # Trades
    def save_trade(self, trade: Trade) -> None
    def load_trades(self, limit: int = 100) -> List[Trade]

    # Balance
    def save_balance(self, balance: float, timestamp: datetime) -> None
    def load_latest_balance(self) -> Optional[float]

    # Stats
    def compute_stats(self) -> StatsSnapshot
```

---

### `src/api/auth.py` + `websocket.py` + `routes.py` + `server.py`

```python
# auth.py — сессионная аутентификация (cookie)
_STORE: dict = {}   # token → username (in-memory, сбрасывается при рестарте)

def create_session(username: str) -> str       # secrets.token_urlsafe(32) → сохранить в _STORE
def delete_session(token: str) -> None
def get_session_user(token: str) -> Optional[str]
def check_credentials(username, password, shared) -> bool   # bcrypt.checkpw из конфига

class SessionAuth:
    # FastAPI dependency: проверяет Cookie "lz_session" → username или HTTP 401
    def __call__(self, lz_session: Optional[str] = Cookie(default=None)) -> str

# websocket.py
class WebSocketManager:
    _connections: Set[WebSocket]
    _log_cursor: int              # индекс последнего отправленного лога

    async def connect(self, ws: WebSocket) -> None    # accept + _push_snapshot
    async def disconnect(self, ws: WebSocket) -> None
    async def broadcast(self, event: str, data: Any) -> None
    async def push_loop(self) -> None                 # asyncio task, каждые 3 сек
    async def _push_snapshot(self, ws: WebSocket) -> None  # полное состояние новому клиенту
    async def _push_periodic(self) -> None            # status + markets + new_logs
    async def _push_new_logs(self) -> None            # только новые записи (по _log_cursor)

# WebSocket события:
# connect    → snapshot: status, stats, markets, positions, history, logs
# push_loop  → status, markets (каждые 3 сек)
# push_loop  → logs_append (только новые записи)

# routes.py — create_router(shared, auth: SessionAuth, loader) → APIRouter(prefix="/api")
# Все /api/* защищены SessionAuth (Depends(auth) → username str | HTTP 401)
# ВАЖНО: from __future__ import annotations НЕ используется в routes.py —
#   иначе local type alias Username = Annotated[str, Depends(auth)] становится строкой,
#   get_type_hints() не находит её в глобальном namespace и FastAPI трактует
#   username как обычный query-параметр (422 вместо авторизации)
GET   /api/status     → BotStatus (dict)
GET   /api/markets    → List[market_summary]
GET   /api/positions  → List[Position]
GET   /api/history    → List[Trade]  (limit: int = 100)
GET   /api/stats      → StatsSnapshot
GET   /api/logs       → List[LogEntry] (limit: int = 200)
GET   /api/whoami     → {"username": str}
POST  /api/start      → loader.patch({"active": True}) → {"ok": true}
POST  /api/stop       → loader.patch({"active": False}) → {"ok": true}
GET   /api/config     → dataclasses.asdict(shared.get_config_snapshot())
POST  /api/config     → validate via loader._parse() → loader.save_full(data)
PATCH /api/config     → loader.patch(data)
WS    /ws             → cookie проверяется при handshake → close(1008) если нет сессии

# server.py — порядок регистрации маршрутов (важен для catch-all):
# 1. GET/POST /login  — публичные (форма входа)
# 2. GET /logout      — публичный (чистит cookie, редиректит на /login)
# 3. /api/*           — SessionAuth (401 если нет сессии)
# 4. WS /ws           — проверка cookie
# 5. /js, /css        — StaticFiles (публичные: только JS/CSS, не содержат данных)
# 6. GET /            — проверка cookie → 302 /login или index.html
# 7. GET /{path}      — проверка cookie → 302 /login или index.html (SPA fallback)
#
# _FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend" (абсолютный путь)

class APIServer:
    def __init__(self, shared: SharedState, loader: ConfigLoader) -> None
    def run(self) -> None
        # uvicorn.run() с ssl_certfile/ssl_keyfile если оба заданы в конфиге
        # Если SSL не задан — предупреждение в лог, запуск без TLS
```

---

### `src/main.py`

```python
def main() -> None:
    db = DBRepository("data/lizardbot.db")
    db.init_schema()

    initial_config = ConfigLoader("config.json").load()
    shared = SharedState(config=initial_config)

    engine     = BotEngine(shared=shared, db=db)
    cfg_loader = ConfigLoader("config.json", shared=shared)
    api        = APIServer(shared=shared, loader=cfg_loader)

    t1 = Thread(target=engine.run,     name="BotEngine",    daemon=True)
    t2 = Thread(target=api.run,        name="APIServer",    daemon=True)
    t3 = Thread(target=cfg_loader.run, name="ConfigLoader", daemon=True)

    engine._recover_state()   # восстановление состояния перед стартом

    t1.start(); t2.start(); t3.start()

    # Graceful shutdown (SIGINT / SIGTERM)
    ...
```

---

## Database Schema

```sql
CREATE TABLE markets (
    market_id   TEXT    PRIMARY KEY,
    question    TEXT    NOT NULL,
    start_time  INTEGER NOT NULL,   -- unix timestamp
    close_time  INTEGER NOT NULL,
    outcome_yes TEXT    NOT NULL,
    outcome_no  TEXT    NOT NULL,
    status      TEXT    NOT NULL,   -- monitoring|bet_placed|closed|skipped
    signal_fired INTEGER DEFAULT 0,
    signal_time  INTEGER,
    created_at  INTEGER NOT NULL
);

CREATE TABLE prob_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    probability REAL    NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE positions (
    market_id   TEXT    PRIMARY KEY,
    outcome     TEXT    NOT NULL,
    amount      REAL    NOT NULL,
    entry_price REAL    NOT NULL,
    entry_time  INTEGER NOT NULL,
    order_id    TEXT,
    simulation  INTEGER NOT NULL,   -- 0|1
    FOREIGN KEY (market_id) REFERENCES markets(market_id)
);

CREATE TABLE trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id   TEXT    NOT NULL,
    question    TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    amount      REAL    NOT NULL,
    entry_price REAL    NOT NULL,
    entry_time  INTEGER NOT NULL,
    close_time  INTEGER NOT NULL,
    result      TEXT    NOT NULL,   -- won|lost
    pnl         REAL    NOT NULL,
    simulation  INTEGER NOT NULL    -- 0|1
);

CREATE TABLE balance_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    balance     REAL    NOT NULL,
    timestamp   INTEGER NOT NULL
);
```

---

## File Structure

```
LizardBot/
├── src/
│   ├── main.py
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── models.py           # все dataclass-модели
│   │   └── state.py            # SharedState
│   ├── config/
│   │   ├── __init__.py
│   │   ├── models.py           # BotConfig, ServerConfig
│   │   └── loader.py           # ConfigLoader (Thread 3)
│   ├── client/
│   │   ├── __init__.py
│   │   └── polymarket.py       # PolymarketClient
│   ├── bot/
│   │   ├── __init__.py
│   │   ├── engine.py           # BotEngine (Thread 1)
│   │   ├── scanner.py          # MarketScanner
│   │   ├── tracker.py          # MarketTracker
│   │   ├── strategy.py         # LizardStrategy
│   │   └── order_manager.py    # OrderManager
│   ├── db/
│   │   ├── __init__.py
│   │   └── repository.py       # DBRepository
│   └── api/
│       ├── __init__.py
│       ├── server.py           # FastAPI app (Thread 2)
│       ├── routes.py           # HTTP endpoints
│       ├── websocket.py        # WebSocketManager
│       └── auth.py             # BasicAuthMiddleware
├── frontend/
│   ├── login.html              # Отдельная страница входа (<form method="post" action="/login">)
│   ├── index.html              # SPA: 3 таба (Мониторинг / Логи / Настройки)
│   │                           # navbar: статус, баланс, username, /logout, theme toggle
│   ├── js/
│   │   └── app.js              # WS-клиент, рендер, редактор конфига
│   │                           # apiFetch: 401 → location.href='/login'
│   │                           # fetchWhoami() на DOMContentLoaded
│   └── css/
│       └── style.css           # CSS vars: --lz-* (dark в :root, light в [data-bs-theme="light"])
│                               # --bs-* Bootstrap overrides только в [data-bs-theme="dark"]
├── data/
│   └── lizardbot.db            # SQLite
├── logs/
│   └── lizardbot.log
├── config.json                 # ! в .gitignore (содержит api_key)
├── config.example.json         # пример без секретов
├── requirements.txt
├── ARCHITECTURE.md
└── CLAUDE.md
```

---

## config.example.json

Ключевые поля (полный пример в `config.example.json`):

```json
{
  "private_key": "0xYOUR_POLYGON_PRIVATE_KEY",
  "funder_address": "0xYOUR_WALLET_ADDRESS",
  "active": false,
  "simulation_mode": true,
  "vol_threshold": 0.20,
  "lookback_minutes": 30,
  "danger_zone_action": "skip",
  "recovery_action": "enter_if_safe",
  "bet_mode": "fixed",
  "bet_amount": 1.0,
  "market_filters": [
    { "name": "BTC 4h", "series_ticker": "btc-up-or-down-4h", "enabled": true }
  ],
  "server": { "host": "0.0.0.0", "port": 8080 },
  "users": {
    "admin":  "$2b$12$...",
    "BECICI": "$2b$12$..."
  }
}
```

**Запуск/стоп стратегии:** только через `active`. Кнопки на фронте пишут в config.json через `loader.patch()`, ConfigLoader подхватывает изменение на следующей итерации.

**Симуляция:** чекбокс на фронте → `PATCH /api/config` с `{"simulation_mode": true/false}`.
В simulation_mode=True поля `private_key` и `funder_address` не обязательны (`.get()` с дефолтом `""`).
`PolymarketClient` в sim mode создаёт `ClobClient(host=...)` без credentials — только публичные эндпоинты.

**Аутентификация — серверные сессии (cookie):**
- Сессии хранятся в памяти: `_STORE: dict = {token → username}` (сбрасываются при рестарте)
- Токен — `secrets.token_urlsafe(32)`, cookie `lz_session` с флагами `httponly`, `samesite=strict`
- Неавторизованный запрос к странице → `302 /login`, к API → `401`
- WebSocket → `close(1008)` при отсутствии сессии
- Публичны только: `/login`, `/logout`, `/js/*`, `/css/*` (статика без данных)
- Страница входа `frontend/login.html` — отдельный HTML с `<form method="post" action="/login">`
- После входа сервер ставит cookie и делает `303 /`; после выхода удаляет cookie и делает `302 /login`
- Фронт (`app.js`): `apiFetch()` не добавляет заголовков (cookie отправляется браузером автоматически);
  при `401` → `location.href = '/login'`; на DOMContentLoaded → `fetchWhoami()` для отображения имени
