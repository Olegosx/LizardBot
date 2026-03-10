"""Auth: серверные сессии (cookie) + проверка bcrypt-паролей."""
import logging
import secrets
from typing import Optional

import bcrypt
from fastapi import Cookie, HTTPException, status

from src.shared.state import SharedState

logger = logging.getLogger(__name__)

SESSION_COOKIE = "lz_session"
_STORE: dict = {}  # token → username


def create_session(username: str) -> str:
    """Создаёт случайный токен, сохраняет в памяти, возвращает токен."""
    token = secrets.token_urlsafe(32)
    _STORE[token] = username
    return token


def delete_session(token: str) -> None:
    """Удаляет сессию по токену."""
    _STORE.pop(token, None)


def get_session_user(token: str) -> Optional[str]:
    """Возвращает username по токену или None."""
    return _STORE.get(token)


def check_credentials(username: str, password: str, shared: SharedState) -> bool:
    """Проверяет логин/пароль по bcrypt-хэшам из текущего конфига."""
    config = shared.get_config_snapshot()
    hashed = config.users.get(username)
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


class SessionAuth:
    """FastAPI dependency: проверяет cookie lz_session → username или HTTP 401."""

    def __call__(self, lz_session: Optional[str] = Cookie(default=None)) -> str:
        if lz_session:
            username = get_session_user(lz_session)
            if username:
                return username
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
        )
