"""HTTP маршруты FastAPI."""
import dataclasses
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.encoders import jsonable_encoder

from src.api.auth import SessionAuth
from src.config.loader import ConfigLoader
from src.shared.state import SharedState

logger = logging.getLogger(__name__)


def create_router(shared: SharedState, auth: SessionAuth, loader: ConfigLoader) -> APIRouter:
    """Создаёт и возвращает APIRouter со всеми HTTP-маршрутами."""
    router = APIRouter(prefix="/api")
    Username = Annotated[str, Depends(auth)]

    # ── Мониторинг ────────────────────────────────────────────────────────

    @router.get("/status")
    async def get_status(username: Username) -> dict:
        return jsonable_encoder(dataclasses.asdict(shared.get_status()))

    @router.get("/markets")
    async def get_markets(username: Username) -> list:
        from src.api.websocket import _market_summary
        return jsonable_encoder([_market_summary(m) for m in shared.get_all_markets()])

    @router.get("/positions")
    async def get_positions(username: Username) -> list:
        return jsonable_encoder([dataclasses.asdict(p) for p in shared.get_all_positions()])

    @router.get("/history")
    async def get_history(username: Username, limit: int = 100) -> list:
        return jsonable_encoder([dataclasses.asdict(t) for t in shared.get_history(limit)])

    @router.get("/stats")
    async def get_stats(username: Username) -> dict:
        return jsonable_encoder(dataclasses.asdict(shared.get_stats()))

    @router.get("/logs")
    async def get_logs(username: Username, limit: int = 200) -> list:
        return jsonable_encoder([dataclasses.asdict(e) for e in shared.get_logs(limit)])

    @router.get("/whoami")
    async def whoami(username: Username) -> dict:
        return {"username": username}

    # ── Управление ботом через config.active ─────────────────────────────

    @router.post("/start")
    async def start_bot(username: Username) -> dict:
        loader.patch({"active": True})
        logger.info("Команда start от '%s': active=true записан в конфиг", username)
        return {"ok": True}

    @router.post("/stop")
    async def stop_bot(username: Username) -> dict:
        loader.patch({"active": False})
        logger.info("Команда stop от '%s': active=false записан в конфиг", username)
        return {"ok": True}

    # ── Конфигурация ──────────────────────────────────────────────────────

    @router.get("/config")
    async def get_config(username: Username) -> dict:
        config = shared.get_config_snapshot()
        return jsonable_encoder(dataclasses.asdict(config))

    @router.post("/config")
    async def save_config(username: Username, data: Any = Body(...)) -> dict:
        if not isinstance(data, dict):
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Ожидается JSON-объект")
        try:
            loader._parse(data)
        except Exception as exc:
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail=f"Ошибка валидации: {exc}")
        loader.save_full(data)
        logger.info("Конфиг обновлён через API пользователем '%s'", username)
        return {"ok": True}

    @router.patch("/config")
    async def patch_config(username: Username, data: Any = Body(...)) -> dict:
        if not isinstance(data, dict):
            from fastapi import HTTPException
            raise HTTPException(status_code=422, detail="Ожидается JSON-объект")
        loader.patch(data)
        logger.info("Частичный патч конфига от '%s': %s", username, list(data.keys()))
        return {"ok": True}

    return router
