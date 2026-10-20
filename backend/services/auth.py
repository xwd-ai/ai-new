import hashlib
import time
import uuid
from typing import Optional

from backend.database import SessionLocal
from backend.models import Session, User

# 用户与会话持久化到 SQLite 数据库
# 密码用简单 hash 存，避免明文；demo 阶段不做加盐/加密
# 预置用户逻辑收敛到 backend.seed 模块

_TOKEN_TTL = 60 * 60 * 24  # 24 小时


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


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
