import hashlib
import time
import uuid
from typing import Optional

from backend.database import SessionLocal
from backend.models import Session, User

# 用户与会话持久化到 SQLite 数据库
# 密码用简单 hash 存，避免明文；demo 阶段不做加盐/加密

_TOKEN_TTL = 60 * 60 * 24  # 24 小时

# 预置用户：首次启动时若用户表为空则写入
_SEED_USERS = [
    {
        "id": "u-admin",
        "username": "admin",
        "password": "admin123",
        "name": "管理员",
        "role": "admin",
    },
    {
        "id": "u-customer",
        "username": "customer",
        "password": "customer123",
        "name": "李女士",
        "role": "customer",
    },
]


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def seed_users() -> None:
    """预置演示用户（幂等：已有用户则跳过）。"""
    db = SessionLocal()
    try:
        if db.query(User).first() is not None:
            return
        for u in _SEED_USERS:
            db.add(
                User(
                    id=u["id"],
                    username=u["username"],
                    password_hash=_hash(u["password"]),
                    name=u["name"],
                    role=u["role"],
                )
            )
        db.commit()
    finally:
        db.close()


def find_by_username(username: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        return u.to_dict() if u else None
    finally:
        db.close()


def authenticate(username: str, password: str) -> Optional[dict]:
    u = find_by_username(username)
    if u and u["password_hash"] == _hash(password):
        return u
    return None


def create_session(user: dict) -> str:
    token = uuid.uuid4().hex
    db = SessionLocal()
    try:
        db.add(
            Session(
                token=token,
                user_id=user["id"],
                username=user["username"],
                name=user["name"],
                role=user["role"],
                expires_at=time.time() + _TOKEN_TTL,
            )
        )
        db.commit()
    finally:
        db.close()
    return token


def get_session(token: str) -> Optional[dict]:
    db = SessionLocal()
    try:
        s = db.query(Session).filter(Session.token == token).first()
        if not s:
            return None
        if s.expired:
            db.delete(s)
            db.commit()
            return None
        return s.to_dict()
    finally:
        db.close()


def public_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
    }
