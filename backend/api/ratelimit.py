"""对话接口限流：固定窗口计数，缓存不可用时放行。

限流针对模型调用这类高成本接口。设计取舍：

- **缓存不可用即放行**：限流是保护措施，不能因为缓存故障把正常请求挡在门外；
- **按用户维度计数**：已登录按 user_id，匿名请求共享一个计数桶
  （匿名无法稳定识别个体，而成本主要来自模型调用）；
- **固定窗口而非滑动窗口**：Redis 侧只需一次 INCR，实现简单且行为可解释；
  代价是窗口边界处可能出现短时双倍通过，对「防滥用」这个目标可以接受。
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from backend.api.auth import current_user
from backend.services import cache

_WINDOW_SECONDS = 60
_DEFAULT_LIMIT = 10


def _limit() -> int:
    """每分钟允许的请求数；非法配置回退默认值。"""
    raw = os.getenv("RATE_LIMIT_CHAT_PER_MINUTE", "")
    if not raw.strip():
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_LIMIT


def chat_rate_limit(
    request: Request,
    user: dict | None = Depends(current_user),
) -> None:
    """对话接口限流依赖：超出限额抛 429；缓存不可用时直接放行。

    作为 FastAPI 依赖使用，与路由的 current_user 依赖共享同一次解析结果。
    """
    enforce_chat_rate(user["id"] if user else "anonymous")


def enforce_chat_rate(identity: str) -> None:
    limit = _limit()
    key = f"rate:chat:{identity}"

    count = cache.incr_with_window(key, _WINDOW_SECONDS)
    if count is None:
        # 缓存不可用：不做限流，保证可用性优先
        return

    if count > limit:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请稍后再试（每分钟上限 {limit} 次）",
            headers={"Retry-After": str(_WINDOW_SECONDS)},
        )
