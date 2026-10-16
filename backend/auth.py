import time
import uuid
from typing import Optional

import hashlib

from backend import store

# 用户表（demo 阶段写死；后续可迁移到持久化）
# 密码用简单 hash 存，避免明文；demo 阶段不做加盐/加密
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
    data = store.read()
    data["sessions"][token] = {
        "user_id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "expires_at": time.time() + _TOKEN_TTL,
    }
    store.write(data)
    return token


def get_session(token: str) -> Optional[dict]:
    data = store.read()
    s = data["sessions"].get(token)
    if not s:
        return None
    if s["expires_at"] < time.time():
        data["sessions"].pop(token, None)
        store.write(data)
        return None
    return s


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
    }
