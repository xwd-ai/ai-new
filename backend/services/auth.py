"""认证与会话服务。

用户查询、密码校验与会话签发都在这一层收口：
- 用户信息从 users 表读取，对外只暴露 id / username / name / role，密码哈希不出本层；
- 密码比对委托 User.check_password：bcrypt 哈希按算法校验，仍为旧版摘要的账号按兼容逻辑比对；
- 校验通过的账号若仍存旧版摘要，或哈希成本低于当前配置，趁这次登录把字段原地升级，
  存量用户无需重置密码即可完成迁移；
- 登录成功后签发随机 token，会话写入 sessions 表，超过 TTL 视为过期。

明文密码只作为 authenticate 的入参存在，不落库、不进日志、不随用户信息返回。
"""

import logging
import time
import uuid
from typing import Optional

from sqlalchemy.exc import SQLAlchemyError

from backend.database import SessionLocal
from backend.models import Session, User
from backend.services.password import PasswordHashError, verify_password

logger = logging.getLogger(__name__)

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


def _upgrade_password(db, user: User, password: str) -> bool:
    """登录成功后把旧强度密码字段升级为当前算法与成本，返回是否发生改写。

    只在字段确需升级时动手：旧版摘要、或 bcrypt 成本低于当前配置。
    升级是登录的附带动作，任何失败都只回滚本次写入、不影响登录结果，
    下一次登录会重新尝试，用户不会因为迁移失败而登不进来。
    """
    if not user.password_needs_upgrade:
        return False
    try:
        user.set_password(password)
        db.commit()
    except PasswordHashError:
        # 旧摘要允许超长密码，bcrypt 不接受超过 72 字节的输入；
        # 这类账号保持原状，登录本身仍然成功
        db.rollback()
        logger.warning("密码字段未升级：密码长度超出当前算法上限 username=%s", user.username)
        return False
    except SQLAlchemyError:
        db.rollback()
        logger.warning("密码字段升级写入失败 username=%s", user.username, exc_info=True)
        return False
    return True


def authenticate(username: str, password: str) -> Optional[dict]:
    """校验用户名与密码，通过返回用户信息，失败返回 None。

    账号不存在、密码不匹配、入参为空或类型不对，一律返回 None，
    调用方无法从返回值区分是哪一种失败。
    校验通过且密码字段仍是旧版摘要时，顺手把它升级为 bcrypt 哈希，
    实现存量账号首登自动迁移；升级结果不改变本次登录的成功与否。
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
        # 先取出对外字段，升级写库失败回滚后 ORM 对象会失效
        account = _account(user)
        _upgrade_password(db, user, password)
        return account
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
