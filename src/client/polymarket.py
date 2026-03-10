"""Клиент Polymarket: Gamma API (поиск рынков) + CLOB API (котировки, ордера)."""
from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, MarketOrderArgs
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

from src.config.models import BotConfig

logger = logging.getLogger(__name__)

_GAMMA_BASE = "https://gamma-api.polymarket.com"
_CLOB_HOST = "https://clob.polymarket.com"


class PolymarketError(Exception):
    """Базовое исключение клиента Polymarket."""


class PolymarketNetworkError(PolymarketError):
    """Ошибка сети или HTTP при запросе к API."""


class PolymarketOrderError(PolymarketError):
    """Ошибка размещения ордера."""


def _parse_json_field(value: Any) -> list:
    """Парсит JSON-строку или возвращает список как есть."""
    if isinstance(value, str):
        return json.loads(value)
    return value if value is not None else []


class GammaClient:
    """Клиент Gamma API (gamma-api.polymarket.com).

    Используется для поиска рынков, метаданных и результатов.
    Авторизация не требуется.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def get_markets_by_series(self, series_ticker: str) -> List[dict]:
        """Возвращает все незакрытые рынки указанной серии.

        Использует /series?slug=... для получения event-тикеров,
        затем /markets?slug=TICKER для каждого незакрытого события.
        (Параметр series_slug в /markets Gamma API игнорирует.)
        """
        try:
            resp = self._session.get(
                f"{_GAMMA_BASE}/series",
                params={"slug": series_ticker},
                timeout=10,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PolymarketNetworkError(f"Gamma API /series недоступен: {exc}") from exc

        data = resp.json()
        series = data[0] if isinstance(data, list) and data else data
        if not isinstance(series, dict):
            logger.warning("Серия '%s' не найдена в Gamma API", series_ticker)
            return []

        open_events = [
            e for e in series.get("events", []) if not e.get("closed", True)
        ]
        if not open_events:
            logger.info("Нет открытых событий в серии '%s'", series_ticker)
            return []

        markets: List[dict] = []
        for event in open_events:
            slug = event.get("slug") or event.get("ticker")
            if not slug:
                continue
            try:
                resp = self._session.get(
                    f"{_GAMMA_BASE}/markets",
                    params={"slug": slug},
                    timeout=10,
                )
                resp.raise_for_status()
                market_data = resp.json()
                if isinstance(market_data, list) and market_data:
                    markets.append(market_data[0])
                elif isinstance(market_data, dict):
                    markets.append(market_data)
            except requests.RequestException as exc:
                logger.warning("Не удалось получить рынок '%s': %s", slug, exc)

        return markets

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Возвращает рынок по slug или None если не найден."""
        try:
            resp = self._session.get(
                f"{_GAMMA_BASE}/markets", params={"slug": slug}, timeout=10
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PolymarketNetworkError(f"Gamma API недоступен: {exc}") from exc

        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data if isinstance(data, dict) else None


class PolymarketClient:
    """Единый клиент Polymarket: Gamma для поиска, CLOB для торговли.

    Инициализируется один раз при старте бота. Потокобезопасен
    при условии, что методы не изменяют внутреннее состояние.
    """

    def __init__(self, config: BotConfig) -> None:
        self._gamma = GammaClient()
        if config.simulation_mode:
            # В режиме симуляции создаём read-only клиент без credentials.
            # get_midpoint (вероятности) — публичный эндпоинт, не требует авторизации.
            # place_order никогда не вызывается, get_balance фолбэчит на $100.
            self._clob = ClobClient(host=_CLOB_HOST)
            logger.info("CLOB API: read-only режим (simulation_mode=True)")
        else:
            self._clob = ClobClient(
                host=_CLOB_HOST,
                key=config.private_key,
                chain_id=POLYGON,
                signature_type=1,
                funder=config.funder_address,
            )
            self._init_clob_creds(config)

    def _init_clob_creds(self, config: BotConfig) -> None:
        """Устанавливает CLOB API credentials: из конфига или деривирует."""
        if config.api_key:
            # Используем готовые credentials из config.json
            from py_clob_client.clob_types import ApiCreds
            self._clob.set_api_creds(ApiCreds(
                api_key=config.api_key,
                api_secret=config.api_secret,
                api_passphrase=config.api_passphrase,
            ))
            logger.info("CLOB API: используем credentials из конфига")
        else:
            # Деривируем credentials из приватного ключа
            creds = self._clob.create_or_derive_api_creds()
            self._clob.set_api_creds(creds)
            logger.info("CLOB API: credentials деривированы из приватного ключа")

    # ── Методы поиска рынков (Gamma API) ──────────────────────────────────

    def get_active_markets(self, series_ticker: str) -> List[dict]:
        """Возвращает все незакрытые рынки серии."""
        return self._gamma.get_markets_by_series(series_ticker)

    def get_market_by_slug(self, slug: str) -> Optional[dict]:
        """Возвращает данные рынка по slug."""
        return self._gamma.get_market_by_slug(slug)

    def get_market_result(self, slug: str) -> Optional[str]:
        """Возвращает название победившего исхода или None если рынок ещё открыт."""
        market = self._gamma.get_market_by_slug(slug)
        if not market or not market.get("closed", False):
            return None

        outcomes = _parse_json_field(market.get("outcomes", []))
        prices = _parse_json_field(market.get("outcomePrices", []))
        for outcome, price in zip(outcomes, prices):
            if float(price) == 1.0:
                return str(outcome)

        logger.warning("Рынок %s закрыт, но победитель не определён", slug)
        return None

    # ── Методы котировок (CLOB API) ────────────────────────────────────────

    def get_market_probability(self, token_id: str) -> Optional[float]:
        """Возвращает текущую вероятность исхода (midpoint CLOB orderbook).

        Возвращает None при недоступности котировки.
        """
        try:
            result = self._clob.get_midpoint(token_id=token_id)
            return float(result.get("mid", 0))
        except Exception as exc:
            logger.warning("Не удалось получить midpoint для %s: %s", token_id[:16], exc)
            return None

    def get_balance(self) -> float:
        """Возвращает USDC-баланс кошелька с Polymarket."""
        try:
            result = self._clob.get_balance_allowance(
                params=BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=1,
                )
            )
            return float(result.get("balance", 0.0))
        except Exception as exc:
            raise PolymarketError(f"Не удалось получить баланс: {exc}") from exc

    # ── Методы ордеров (CLOB API) ──────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        amount: float,
        neg_risk: bool,
        order_min_size: float,
    ) -> dict:
        """Размещает рыночный ордер на покупку.

        Args:
            token_id: clobTokenId исхода для ставки.
            amount: Сумма в USDC.
            neg_risk: Флаг negRisk рынка.
            order_min_size: Минимальный размер ордера (из метаданных рынка).

        Returns:
            Ответ CLOB API с order_id и статусом.

        Raises:
            PolymarketOrderError: При недопустимой сумме или ошибке API.
        """
        if amount < order_min_size:
            raise PolymarketOrderError(
                f"Ставка {amount} USDC меньше минимальной {order_min_size} USDC"
            )
        try:
            from py_clob_client.clob_types import OrderType
            tick_size = self._clob.get_tick_size(token_id=token_id)
            order_args = MarketOrderArgs(token_id=token_id, amount=amount, side=BUY)
            signed = self._clob.create_market_order(
                order_args=order_args,
                options={"tick_size": tick_size, "neg_risk": neg_risk},
            )
            return self._clob.post_order(signed, OrderType.FOK)
        except PolymarketOrderError:
            raise
        except Exception as exc:
            raise PolymarketOrderError(f"Ошибка размещения ордера: {exc}") from exc
