import os
import time
import uuid
from typing import Optional

# 内存用户表（demo 阶段不引数据库）
# 密码用简单 hash 存，避免明文；demo 阶段不做加盐/加密
import hashlib

_USERS = [
    {
        "id": "u-admin",
        "username": "admin",
        "password_hash": hashlib.sha256("admin123".encode()).hexdigest(),
        "name": "管理员",
        "role": "admin",
    },
    {
        "id": "u-customer",
        "username": "customer",
        "password_hash": hashlib.sha256("customer123".encode()).hexdigest(),
        "name": "李女士",
        "role": "customer",
    },
]

# token -> user_id，内存会话
_SESSIONS: dict[str, dict] = {}

_TOKEN_TTL = 60 * 60 * 24  # 24 小时


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def find_by_username(username: str) -> Optional[dict]:
    for u in _USERS:
        if u["username"] == username:
            return u
    return None


def authenticate(username: str, password: str) -> Optional[dict]:
    u = find_by_username(username)
    if u and u["password_hash"] == _hash(password):
        return u
    return None


def create_session(user: dict) -> str:
    token = uuid.uuid4().hex
    _SESSIONS[token] = {
        "user_id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "expires_at": time.time() + _TOKEN_TTL,
    }
    return token


def get_session(token: str) -> Optional[dict]:
    s = _SESSIONS.get(token)
    if not s:
        return None
    if s["expires_at"] < time.time():
        _SESSIONS.pop(token, None)
        return None
    return s


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
    }
