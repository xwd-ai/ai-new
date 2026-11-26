"""Redis 分布式锁：跨进程 / 跨实例的互斥，缓存不可用时降级为无锁。

实现只用两条 Redis 原语，够用且不容易出错：

- **加锁**：``SET key token NX PX ttl`` —— NX 保证只有一方成功，PX 保证进程
  崩溃后锁能自动过期，不会永久死锁；
- **释放**：Lua 脚本先比对 token 再删除 —— 避免「自己的锁已超时、别人重新
  持锁」时误删他人的锁，这是分布式锁最常见的坑。

**边界说明**：本锁只减少并发冲突，不承担最终一致性。建单的幂等仍由
``dedup_key`` 唯一约束兜底，因此缓存不可用时直接放行是安全的。
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from backend.services import cache

# 比对 token 后再删除，保证只释放自己持有的锁
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RETRY_INTERVAL = 0.02


@contextmanager
def distributed_lock(
    key: str,
    ttl_ms: int = 5000,
    wait_seconds: float = 5.0,
) -> Iterator[bool]:
    """尝试获取分布式锁，yield 是否持有。

    - 缓存不可用：yield False 并直接放行（调用方按无锁路径处理）；
    - 等待超时：同样 yield False，调用方应依赖唯一约束兜底；
    - 持有期间抛异常：finally 中释放，异常继续向上抛。
    """
    client = cache.client()
    if client is None:
        yield False
        return

    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait_seconds
    acquired = False

    while time.monotonic() < deadline:
        try:
            if client.set(key, token, nx=True, px=ttl_ms):
                acquired = True
                break
        except Exception:  # noqa: BLE001 - 缓存抖动时按未获取处理
            break
        time.sleep(_RETRY_INTERVAL)

    if not acquired:
        yield False
        return

    try:
        yield True
    finally:
        try:
            client.eval(_RELEASE_SCRIPT, 1, key, token)
        except Exception:  # noqa: BLE001 - 释放失败由 TTL 兜底
            pass
