"""LizardBot — точка входа. Запускает три потока и ждёт сигнала остановки."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

CONFIG_PATH = "config.json"
DB_PATH = "data/lizardbot.db"
LOG_PATH = "logs/lizardbot.log"


def setup_logging(level: str, shared: object) -> None:
    """Настраивает вывод логов: консоль, файл и SharedState-буфер."""
    from src.shared.log_handler import SharedLogHandler

    fmt = "%(asctime)s [%(levelname)-8s] [%(threadName)s] %(name)s: %(message)s"
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not root.handlers:
        Path("logs").mkdir(exist_ok=True)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(fmt))
        root.addHandler(console)

        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(fmt))
        root.addHandler(file_handler)

    shared_handler = SharedLogHandler(shared)
    shared_handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(shared_handler)


def main() -> None:
    """Инициализирует компоненты и запускает потоки."""
    from src.bot.engine import BotEngine
    from src.client.polymarket import PolymarketClient
    from src.config.loader import ConfigLoader
    from src.db.repository import DBRepository
    from src.shared.state import SharedState

    Path("data").mkdir(exist_ok=True)
    Path("logs").mkdir(exist_ok=True)

    # Загружаем конфиг
    loader = ConfigLoader(CONFIG_PATH)
    try:
        config = loader.load()
    except FileNotFoundError:
        print(f"[ERROR] Конфиг не найден: {CONFIG_PATH}")
        print(f"        Скопируйте config.example.json → {CONFIG_PATH} и заполните")
        sys.exit(1)
    except Exception as exc:
        print(f"[ERROR] Ошибка чтения конфига: {exc}")
        sys.exit(1)

    # SharedState (нужен до setup_logging для SharedLogHandler)
    db = DBRepository(DB_PATH)
    db.init_schema()
    shared = SharedState(config=config)

    setup_logging(config.log_level, shared)
    logger = logging.getLogger(__name__)
    mode = "SIMULATION" if config.simulation_mode else "LIVE TRADING"
    logger.info("=" * 60)
    logger.info("LizardBot запускается | Режим: %s", mode)
    logger.info("=" * 60)

    # Клиент Polymarket (инициализируется один раз, используется обоими потоками)
    try:
        client = PolymarketClient(config)
    except Exception as exc:
        logger.error("Не удалось инициализировать Polymarket клиент: %s", exc)
        sys.exit(1)

    # Компоненты
    from src.api.server import APIServer
    engine = BotEngine(client=client, shared=shared, db=db)
    config_loader = ConfigLoader(CONFIG_PATH, shared=shared)
    api_server = APIServer(shared=shared, loader=config_loader)
    stop_event = threading.Event()

    # Graceful shutdown
    def shutdown(signum: int, _frame: object) -> None:
        logger.info("Получен сигнал %s, завершаем работу...", signum)
        engine.stop()
        stop_event.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Запуск потоков
    t1 = threading.Thread(target=engine.run,       name="BotEngine",    daemon=True)
    t2 = threading.Thread(target=api_server.run,   name="APIServer",    daemon=True)
    t3 = threading.Thread(target=config_loader.run, name="ConfigLoader", daemon=True)

    t1.start()
    t2.start()
    t3.start()
    logger.info("Потоки запущены: BotEngine, APIServer, ConfigLoader")

    stop_event.wait()
    logger.info("LizardBot завершил работу")


if __name__ == "__main__":
    main()
