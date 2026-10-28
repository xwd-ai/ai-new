"""结构化日志。

此前排查问题只能靠临时打印或翻代码推断，一旦线上出问题就没有痕迹可看。
这里提供一层轻量的结构化日志：每条记录带固定的字段（事件名、耗时、
结果），既不依赖额外的日志框架，也便于后续接入集中采集。

两个设计取舍：

- **默认不输出调试细节**：日志级别由环境变量控制，默认只记概要。
  在开发机上打开详细日志，线上保持安静，避免日志本身成为负担。
- **记录耗时而非时间戳**：检索、重排、工具调用这类操作，关心的
  「这一步花了多久」比「几点几分发生的」更有用；时间戳由日志框架补。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Any

# 日志器名称固定，便于统一配置与过滤
LOGGER_NAME = "app"

# 环境变量：LOG_LEVEL 控制级别，LOG_FORMAT 选择 plain/json
_DEFAULT_LEVEL = "INFO"


def _resolve_level() -> int:
    """解析日志级别；非法值退回 INFO。"""
    raw = (os.getenv("LOG_LEVEL") or _DEFAULT_LEVEL).strip().upper()
    level = getattr(logging, raw, None)
    if isinstance(level, int):
        return level
    return logging.INFO


def _use_json() -> bool:
    """是否输出 JSON 格式。

    JSON 便于机器采集与检索；本地开发时纯文本更易读，因此默认纯文本，
    需要接入日志平台时再切 JSON。
    """
    return (os.getenv("LOG_FORMAT") or "").strip().lower() == "json"


class _PlainFormatter(logging.Formatter):
    """纯文本格式：时间 级别 事件 键值对。"""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {record.getMessage()}"
        extra = getattr(record, "fields", None)
        if extra:
            pairs = " ".join(f"{k}={v}" for k, v in extra.items())
            return f"{base} | {pairs}"
        return base


class _JsonFormatter(logging.Formatter):
    """JSON 格式：一行一个对象，便于采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        # 已配置过：避免重复添加 handler 造成日志翻倍
        return logger
    logger.setLevel(_resolve_level())
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if _use_json() else _PlainFormatter())
    logger.addHandler(handler)
    return logger


def get_logger() -> logging.Logger:
    """取得应用日志器。"""
    return _build_logger()


def log_event(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """记录一条结构化事件。

    字段值会原样保留（数字不转字符串），便于后续按耗时、条数做统计。
    """
    logger = get_logger()
    # 只保留有值的字段，避免日志里出现一堆 None
    cleaned = {k: v for k, v in fields.items() if v is not None}
    logger.log(level, event, extra={"fields": cleaned})


@contextmanager
def timed(event: str, **fields: Any):
    """为一段操作计时，结束时记录耗时与结果。

    用法::

        with timed("retrieval.search", query=q) as result:
            result["count"] = len(items)

    无论成功或抛异常都会记录：失败时额外记下异常类型，便于统计失败率。
    耗时按毫秒取整——更细的精度对排查没有帮助，反而让日志噪声变大。
    """
    started = time.perf_counter()
    result: dict[str, Any] = {}
    try:
        yield result
    except Exception as exc:  # noqa: BLE001 - 记录后原样抛出，不吞异常
        elapsed = int((time.perf_counter() - started) * 1000)
        log_event(event, level=logging.ERROR, duration_ms=elapsed, error=type(exc).__name__, **fields, **result)
        raise
    else:
        elapsed = int((time.perf_counter() - started) * 1000)
        log_event(event, duration_ms=elapsed, **fields, **result)
