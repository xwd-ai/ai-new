"""Redis 缓存：可选能力，未配置或不可用时所有操作静默降级。

设计原则：
- **缓存只做加速，不承担正确性** —— 任何 Redis 异常都回退到数据库查询，
  绝不让缓存不可用变成业务不可用；
- 未设置 ``REDIS_URL`` 时完全不加载 redis 客户端，保持「零外部依赖可运行」；
- 连接与读写超时都设得很短（1 秒），避免 Redis 抖动拖慢主流程。

调用方约定：读缓存命中返回字符串，未命中或不可用返回 ``None``，
因此「缓存未命中」与「缓存不可用」对调用方是同一种处理路径。
"""

from __future__ import annotations

import os
from typing import Optional

_client = None
_initialized = False


def _get_client():
    """惰性建立连接：首次调用时按 REDIS_URL 尝试连接，失败即视为未启用。"""
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True

    url = (os.getenv("REDIS_URL") or "").strip()
    if not url:
        return None

    try:
        import redis  # 延迟导入：未启用缓存时不要求安装该依赖

        client = redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
        client.ping()
        _client = client
    except Exception:  # noqa: BLE001 - 连不上就当作未启用，不影响业务
        _client = None
    return _client


def enabled() -> bool:
    """缓存是否可用（配置了 REDIS_URL 且连接成功）。"""
    return _get_client() is not None


def get(key: str) -> Optional[str]:
    """读取缓存；未命中、不可用或异常一律返回 None。"""
    client = _get_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception:  # noqa: BLE001
        return None


def set(key: str, value: str, ttl: Optional[int] = None) -> bool:
    """写入缓存；未启用或异常返回 False（调用方可忽略该结果）。"""
    client = _get_client()
    if client is None:
        return False
    try:
        if ttl:
            client.setex(key, ttl, value)
        else:
            client.set(key, value)
        return True
    except Exception:  # noqa: BLE001
        return False


def delete(*keys: str) -> int:
    """删除若干键；未启用或异常返回 0。"""
    client = _get_client()
    if client is None or not keys:
        return 0
    try:
        return int(client.delete(*keys))
    except Exception:  # noqa: BLE001
        return 0


def client():
    """返回底层客户端，供需要 Lua 脚本等高级能力的模块使用；不可用时为 None。"""
    return _get_client()


def incr(key: str) -> Optional[int]:
    """自增计数（不设过期），用于版本号这类单调递增标记。

    返回自增后的值；缓存不可用或异常时返回 None。
    """
    client = _get_client()
    if client is None:
        return None
    try:
        return int(client.incr(key))
    except Exception:  # noqa: BLE001
        return None


def incr_with_window(key: str, window: int) -> Optional[int]:
    """固定窗口计数（限流用）：自增并仅在首次写入时设置过期。

    返回自增后的计数值；缓存不可用时返回 None，调用方据此跳过限流
    —— 宁可放行也不因为缓存故障拒绝正常请求。
    """
    client = _get_client()
    if client is None:
        return None
    try:
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, window)
        return count
    except Exception:  # noqa: BLE001
        return None


def reset() -> None:
    """重置连接状态（仅供测试在切换 REDIS_URL 后重新探测）。"""
    global _client, _initialized
    _client = None
    _initialized = False
