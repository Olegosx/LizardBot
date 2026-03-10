"""Модели конфигурации бота."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class ServerConfig:
    """Настройки HTTP/WebSocket сервера."""

    host: str = "0.0.0.0"
    port: int = 8443
    ssl_certfile: str = ""   # Путь к SSL-сертификату (PEM)
    ssl_keyfile: str = ""    # Путь к приватному ключу (PEM)


@dataclass
class MarketFilterConfig:
    """Настройки фильтра рынков для мониторинга.

    Добавление нового типа рынков (например ETH) — это новый объект
    MarketFilterConfig с другим series_ticker в конфиге.
    """

    name: str               # Человекочитаемое название, e.g. "BTC 4h"
    series_ticker: str      # Серия на Polymarket, e.g. "btc-up-or-down-4h"
    enabled: bool = True


@dataclass
class BotConfig:
    """Полная конфигурация бота, читается из config.json."""

    # Polymarket аутентификация
    private_key: str        # Приватный ключ Polygon-кошелька (для подписи ордеров)
    api_key: str            # CLOB API key (опционально: если пусто — деривируется)
    api_secret: str         # CLOB API secret
    api_passphrase: str     # CLOB API passphrase
    funder_address: str     # Адрес кошелька на Polygon (USDC-баланс)

    # Режим работы
    active: bool = False            # True = стратегия активна (запуск через конфиг)
    simulation_mode: bool = True    # True = эмуляция, False = реальные сделки

    # Стратегия (Hypothesis 1)
    vol_threshold: float = 0.20             # Порог std dev вероятности
    lookback_minutes: int = 30              # Окно наблюдения (мин)
    danger_zone_action: str = "skip"        # skip | reduce | trade
    danger_zone_reduce_factor: float = 0.5  # Коэффициент снижения ставки при "reduce"
    recovery_action: str = "enter_if_safe"  # skip | enter | enter_if_safe

    # Размер ставки
    bet_mode: str = "fixed"     # fixed | percent | double_on_double
    bet_amount: float = 1.0     # Для "fixed" — сумма в USDC
    bet_percent: float = 5.0    # Для "percent" и "double_on_double" — % от баланса

    # Рынки для мониторинга
    market_filters: List[MarketFilterConfig] = field(default_factory=list)

    # Системные
    config_reload_interval: int = 10    # Сек между проверками конфига
    log_level: str = "INFO"

    # Сервер
    server: ServerConfig = field(default_factory=ServerConfig)

    # Пользователи Basic Auth: username -> bcrypt_hash
    users: Dict[str, str] = field(default_factory=dict)
