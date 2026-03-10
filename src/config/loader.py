"""Загрузчик и горячая перезагрузка конфигурации (Thread 3)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import TYPE_CHECKING, Optional

from src.config.models import BotConfig, MarketFilterConfig, ServerConfig

if TYPE_CHECKING:
    from src.shared.state import SharedState

logger = logging.getLogger(__name__)


class ConfigLoader:
    """Читает config.json и при изменении обновляет SharedState.

    Thread 3: запускается в бесконечном цикле, проверяет mtime файла
    каждые config_reload_interval секунд. Остальные потоки вызывают
    shared.get_config_snapshot() перед началом итерации.
    """

    def __init__(self, config_path: str, shared: Optional[SharedState] = None) -> None:
        self.config_path = config_path
        self.shared = shared
        self._last_mtime: float = 0.0

    def load(self) -> BotConfig:
        """Читает и парсит config.json → BotConfig."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._last_mtime = os.path.getmtime(self.config_path)
        return self._parse(data)

    def run(self) -> None:
        """Главный цикл Thread 3: горячая перезагрузка конфига."""
        if self.shared is None:
            raise RuntimeError("SharedState не задан для ConfigLoader")

        interval = self.shared.get_config_snapshot().config_reload_interval
        while True:
            time.sleep(interval)
            try:
                if self._has_changed():
                    config = self.load()
                    self.shared.update_config(config)
                    interval = config.config_reload_interval
                    logger.info("Конфиг перезагружен из %s", self.config_path)
            except Exception as exc:
                logger.error("Ошибка перезагрузки конфига: %s", exc)

    def save_full(self, data: dict) -> None:
        """Записывает полный словарь как config.json (атомарно)."""
        dir_ = os.path.dirname(os.path.abspath(self.config_path))
        with tempfile.NamedTemporaryFile(
            "w", dir=dir_, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = tmp.name
        os.replace(tmp_path, self.config_path)

    def patch(self, updates: dict) -> None:
        """Частично обновляет config.json указанными полями верхнего уровня."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.update(updates)
        self.save_full(data)

    def _has_changed(self) -> bool:
        """Возвращает True если файл конфига изменился с последнего чтения."""
        try:
            return os.path.getmtime(self.config_path) > self._last_mtime
        except OSError:
            return False

    def _parse(self, data: dict) -> BotConfig:
        """Конвертирует raw dict из JSON → BotConfig."""
        return BotConfig(
            private_key=data.get("private_key", ""),
            api_key=data.get("api_key", ""),
            api_secret=data.get("api_secret", ""),
            api_passphrase=data.get("api_passphrase", ""),
            funder_address=data.get("funder_address", ""),
            active=data.get("active", False),
            simulation_mode=data.get("simulation_mode", True),
            vol_threshold=data.get("vol_threshold", 0.20),
            lookback_minutes=data.get("lookback_minutes", 30),
            danger_zone_action=data.get("danger_zone_action", "skip"),
            danger_zone_reduce_factor=data.get("danger_zone_reduce_factor", 0.5),
            recovery_action=data.get("recovery_action", "enter_if_safe"),
            bet_mode=data.get("bet_mode", "fixed"),
            bet_amount=data.get("bet_amount", 1.0),
            bet_percent=data.get("bet_percent", 5.0),
            market_filters=self._parse_filters(data.get("market_filters", [])),
            config_reload_interval=data.get("config_reload_interval", 10),
            log_level=data.get("log_level", "INFO"),
            server=self._parse_server(data.get("server", {})),
            users=data.get("users", {}),
        )

    def _parse_filters(self, raw: list) -> list:
        """Парсит список market_filters из конфига."""
        return [
            MarketFilterConfig(
                name=f.get("name", ""),
                series_ticker=f["series_ticker"],
                enabled=f.get("enabled", True),
            )
            for f in raw
        ]

    def _parse_server(self, raw: dict) -> ServerConfig:
        """Парсит секцию server из конфига."""
        return ServerConfig(
            host=raw.get("host", "0.0.0.0"),
            port=raw.get("port", 8443),
            ssl_certfile=raw.get("ssl_certfile", ""),
            ssl_keyfile=raw.get("ssl_keyfile", ""),
        )
