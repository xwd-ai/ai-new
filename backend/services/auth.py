"""认证与会话服务。

用户查询、密码校验与会话签发都在这一层收口：
- 用户信息从 users 表读取，对外只暴露 id / username / name / role，密码哈希不出本层；
- 密码比对委托 User.check_password：bcrypt 哈希按算法校验，仍为旧版摘要的账号按兼容逻辑比对；
- 登录成功后签发随机 token，会话写入 sessions 表，超过 TTL 视为过期。

明文密码只作为 authenticate 的入参存在，不落库、不进日志、不随用户信息返回。
"""

import time
import uuid
from typing import Optional

from backend.database import SessionLocal
from backend.models import Session, User
from backend.services.password import verify_password

_TOKEN_TTL = 60 * 60 * 24  # 24 小时

# 对外可见的用户字段；密码哈希不在此列，避免随用户信息流到接口与页面
_PUBLIC_FIELDS = ("id", "username", "name", "role")

# 账号不存在时用于消耗一次哈希校验的占位串（cost 与默认成本一致，明文不参与比对），
# 使「账号不存在」与「密码错误」两条失败分支耗时接近，不借响应速度探测账号
_PLACEHOLDER_HASH = "$2b$12$sEi4W.uy8dqAnA/yZybMn.5v344bf3eEqrCiF6wyb0p2AWAQFWIIu"


def _account(user: User) -> dict:
    """把用户记录转成对外字典。"""
    return {field: getattr(user, field) for field in _PUBLIC_FIELDS}


def _query_user(db, username: str) -> Optional[User]:
    """按用户名取用户记录，未命中返回 None。"""
    return db.query(User).filter(User.username == username).first()


def find_by_username(username: str) -> Optional[dict]:
    """按用户名查询用户信息，不存在返回 None。"""
    if not username:
        return None
    db = SessionLocal()
    try:
        user = _query_user(db, username)
        return _account(user) if user else None
    finally:
        db.close()


def authenticate(username: str, password: str) -> Optional[dict]:
    """校验用户名与密码，通过返回用户信息，失败返回 None。

    账号不存在、密码不匹配、入参为空或类型不对，一律返回 None，
    调用方无法从返回值区分是哪一种失败。
    校验过程与登录流程解耦：这里只读不改，登录本身不回写密码字段。
    """
    if not isinstance(username, str) or not isinstance(password, str):
        return None
    if not username or not password:
        return None

    db = SessionLocal()
    try:
        user = _query_user(db, username)
        if user is None:
            verify_password(password, _PLACEHOLDER_HASH)
            return None
        if not user.check_password(password):
            return None
        return _account(user)
    finally:
        db.close()


def create_session(user: dict) -> str:
    """为已通过校验的用户签发会话 token 并落库。"""
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
    """按 token 取会话，过期的会话顺手删除，无效或过期返回 None。"""
    if not token:
        return None
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
    """从用户信息中挑出可公开字段。"""
    return {field: user.get(field) for field in _PUBLIC_FIELDS}
