"""密码哈希工具。

统一负责密码的哈希生成与校验，底层使用 bcrypt：
- 每次哈希自动生成随机盐，同一密码两次哈希结果不同；
- 哈希串自描述算法与成本因子，可直接落库，无需额外字段；
- 成本因子可通过环境变量 BCRYPT_COST 调整，未配置时使用默认值。
"""

import os

import bcrypt

DEFAULT_COST = 12
MIN_COST = 4
MAX_COST = 16

# bcrypt 算法本身只处理前 72 字节，超长密码直接拒绝而不是静默截断
MAX_PASSWORD_BYTES = 72

_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


class PasswordHashError(ValueError):
    """密码无法哈希，或哈希串格式非法。"""


def resolve_cost(cost: int | None = None) -> int:
    """确定本次哈希使用的成本因子。

    优先级：显式入参 > 环境变量 BCRYPT_COST > DEFAULT_COST，
    超出 [MIN_COST, MAX_COST] 的取值会被收敛到区间边界。
    """
    if cost is None:
        raw = os.getenv("BCRYPT_COST", "").strip()
        cost = int(raw) if raw.isdigit() else DEFAULT_COST
    return max(MIN_COST, min(MAX_COST, int(cost)))


def _encode(password: str) -> bytes:
    if not isinstance(password, str):
        raise PasswordHashError("密码必须是字符串")
    if not password:
        raise PasswordHashError("密码不能为空")
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise PasswordHashError(f"密码不能超过 {MAX_PASSWORD_BYTES} 字节")
    return raw


def hash_password(password: str, cost: int | None = None) -> str:
    """生成密码哈希，返回可直接存库的 bcrypt 字符串。"""
    rounds = resolve_cost(cost)
    digest = bcrypt.hashpw(_encode(password), bcrypt.gensalt(rounds))
    return digest.decode()


def is_bcrypt_hash(stored: str | None) -> bool:
    """判断库中存储的字符串是否为结构完整的 bcrypt 哈希。

    bcrypt 哈希形如 ``$2b$12$<53 位盐与摘要>``，总长 60；
    被截断或拼接错误的字符串不算有效哈希。
    """
    if not stored or not isinstance(stored, str):
        return False
    if not stored.startswith(_BCRYPT_PREFIXES):
        return False
    parts = stored.split("$")
    if len(parts) != 4:
        return False
    _, _version, cost, digest = parts
    return cost.isdigit() and len(digest) == 53


def hash_cost(stored: str) -> int | None:
    """从 bcrypt 哈希串中读出成本因子，格式不符时返回 None。"""
    if not is_bcrypt_hash(stored):
        return None
    parts = stored.split("$")
    if len(parts) < 4 or not parts[2].isdigit():
        return None
    return int(parts[2])


def verify_password(password: str, stored: str | None) -> bool:
    """校验密码是否与哈希串匹配。

    仅识别 bcrypt 哈希；其它形式（如旧版摘要）一律返回 False，由调用方决定降级策略。
    密码或哈希串非法时同样返回 False，不抛异常，避免把校验细节暴露给外部。
    """
    if not is_bcrypt_hash(stored):
        return False
    try:
        return bcrypt.checkpw(_encode(password), stored.encode())
    except (ValueError, PasswordHashError):
        return False


def needs_rehash(stored: str | None, cost: int | None = None) -> bool:
    """判断哈希串是否需要按当前配置重新生成。

    非 bcrypt 哈希，或成本因子低于当前配置时返回 True。
    """
    current = hash_cost(stored) if stored else None
    if current is None:
        return True
    return current < resolve_cost(cost)
