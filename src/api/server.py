"""APIServer — FastAPI приложение + запуск через uvicorn (Thread 2)."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from src.api.auth import (
    SESSION_COOKIE, SessionAuth,
    check_credentials, create_session, delete_session, get_session_user,
)
from src.api.routes import create_router
from src.api.websocket import WebSocketManager
from src.config.loader import ConfigLoader
from src.shared.state import SharedState

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"


def create_app(shared: SharedState, loader: ConfigLoader) -> FastAPI:
    """Создаёт и конфигурирует FastAPI приложение."""
    ws_manager = WebSocketManager(shared)
    session_auth = SessionAuth()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        asyncio.create_task(ws_manager.push_loop())
        logger.info("APIServer запущен, WebSocket push_loop активен")
        yield
        logger.info("APIServer останавливается")

    app = FastAPI(title="LizardBot", lifespan=lifespan)

    # ── Авторизация ───────────────────────────────────────────────────────

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token and get_session_user(token):
            return RedirectResponse(url="/", status_code=302)
        return FileResponse(str(_FRONTEND_DIR / "login.html"))

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        if check_credentials(username, password, shared):
            token = create_session(username)
            logger.info("Пользователь '%s' вошёл в систему", username)
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", path="/")
            return resp
        logger.warning("Неудачная попытка входа: пользователь '%s'", username)
        return RedirectResponse(url="/login?error=1", status_code=303)

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            username = get_session_user(token)
            delete_session(token)
            logger.info("Пользователь '%s' вышел из системы", username)
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ── API маршруты ──────────────────────────────────────────────────────

    app.include_router(create_router(shared, session_auth, loader))

    # ── WebSocket ─────────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        token = ws.cookies.get(SESSION_COOKIE)
        if not token or not get_session_user(token):
            await ws.close(code=1008)
            return
        await ws_manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug("WS соединение закрыто: %s", exc)
        finally:
            await ws_manager.disconnect(ws)

    # ── Статические файлы ─────────────────────────────────────────────────

    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Подключает статические файлы и защищённые страницы фронтенда."""
    if not _FRONTEND_DIR.exists():
        logger.warning("Директория frontend/ не найдена, UI недоступен")
        return

    if (_FRONTEND_DIR / "js").exists():
        app.mount("/js", StaticFiles(directory=str(_FRONTEND_DIR / "js")), name="js")
    if (_FRONTEND_DIR / "css").exists():
        app.mount("/css", StaticFiles(directory=str(_FRONTEND_DIR / "css")), name="css")

    @app.get("/")
    async def serve_index(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not get_session_user(token):
            return RedirectResponse(url="/login", status_code=302)
        return FileResponse(str(_FRONTEND_DIR / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if not token or not get_session_user(token):
            return RedirectResponse(url="/login", status_code=302)
        return FileResponse(str(_FRONTEND_DIR / "index.html"))


class APIServer:
    """Обёртка для запуска FastAPI через uvicorn в Thread 2."""

    def __init__(self, shared: SharedState, loader: ConfigLoader) -> None:
        self._shared = shared
        self._loader = loader

    def run(self) -> None:
        """Блокирующий запуск uvicorn (Thread 2 main)."""
        config = self._shared.get_config_snapshot()
        app = create_app(self._shared, self._loader)

        ssl_kwargs: dict = {}
        if config.server.ssl_certfile and config.server.ssl_keyfile:
            ssl_kwargs["ssl_certfile"] = config.server.ssl_certfile
            ssl_kwargs["ssl_keyfile"] = config.server.ssl_keyfile
            proto = "https"
        else:
            proto = "http"
            logger.warning("SSL не настроен — сервер запущен без шифрования")

        logger.info("APIServer слушает на %s://%s:%d", proto, config.server.host, config.server.port)
        uvicorn.run(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level="warning",
            access_log=False,
            **ssl_kwargs,
        )
