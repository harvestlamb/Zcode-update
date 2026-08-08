"""Session-based admin auth for the management UI.

Tokens are random, kept in process memory (a restart logs everyone out — fine
for a single-admin intranet box). The /api/admin/* endpoints (except login) are
guarded by the require_admin dependency, which checks the zc_admin cookie.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Dict

from fastapi import Cookie, Depends, HTTPException, status

import config_store

log = logging.getLogger("zcode-ai.auth")

COOKIE_NAME = "zc_admin"
SESSION_TTL = 60 * 60 * 12  # 12h

# token -> {username, expires_at}
_sessions: Dict[str, dict] = {}


def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"username": username, "expires_at": time.time() + SESSION_TTL}
    _gc_sessions()
    return token


def destroy_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def _gc_sessions() -> None:
    now = time.time()
    expired = [t for t, s in _sessions.items() if s["expires_at"] < now]
    for t in expired:
        _sessions.pop(t, None)


def require_admin(zc_admin: str | None = Cookie(default=None)) -> str:
    """FastAPI dependency: 401 unless a valid session cookie is present."""
    if not zc_admin:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录")
    sess = _sessions.get(zc_admin)
    if not sess or sess["expires_at"] < time.time():
        _sessions.pop(zc_admin, None)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="会话已过期，请重新登录")
    # Sliding expiry.
    sess["expires_at"] = time.time() + SESSION_TTL
    return sess["username"]


def login(username: str, password: str) -> str | None:
    """Validate credentials; return a session token or None on failure."""
    if config_store.verify_credentials(username, password):
        return create_session(username)
    return None
