"""Go 检索服务的客户端。

设计原则与缓存层一致：**远程调用只是加速，不是正确性依赖**。

- 未设置 ``GO_RETRIEVAL_URL`` 时完全不启用，所有调用直接返回 ``None``；
- 配置了但连接失败、超时、返回非法 JSON，同样返回 ``None``；
- 调用方拿到 ``None`` 就退回本地实现，因此 Go 服务宕机不会影响业务。

刻意用标准库 ``urllib``（而非 httpx）：少一个运行期依赖，
且这里只需要「发一个 JSON POST、读一个 JSON」这点能力。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

_URL_ENV = "GO_RETRIEVAL_URL"
# 业务服务（订单/工单等）的地址，与检索服务分开配置：两者可独立部署与回滚
_BUSINESS_URL_ENV = "GO_BUSINESS_URL"
# 超时设得比较短：远程检索只是优化，宁可降级也不要把请求拖住
_TIMEOUT_SECONDS = 2.0

# 本地服务不应走系统代理，否则 127.0.0.1 可能被代理拦下
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base_url() -> Optional[str]:
    url = (os.getenv(_URL_ENV) or "").strip()
    if not url:
        return None
    return url.rstrip("/")


def enabled() -> bool:
    """是否配置了 Go 服务地址。"""
    return _base_url() is not None


def _post(path: str, payload: dict, timeout: float = _TIMEOUT_SECONDS) -> Optional[dict]:
    """向 Go 服务发一次 JSON POST；任何失败都返回 None。"""
    base = _base_url()
    if base is None:
        return None

    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        # 连接失败、超时、非 2xx、响应不是 JSON —— 一律视为不可用
        return None


def search(query: str, docs: list[dict], top_k: int) -> Optional[list[dict]]:
    """调用 Go 的 /search，返回结果列表；不可用时返回 None。

    只传检索必需的字段，避免把整篇正文之外的元数据都塞进请求体。
    """
    if not docs:
        return None

    payload = {
        "query": query,
        "top_k": top_k,
        "docs": [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "content": item.get("content", ""),
                "keywords": item.get("keywords") or [],
            }
            for item in docs
        ],
    }

    data = _post("/search", payload)
    if not data or not isinstance(data.get("results"), list):
        return None
    return data["results"]


def chunk_count(title: str, content: str) -> Optional[int]:
    """调用 Go 的 /ingest 计算分块数；不可用时返回 None。

    只取块数而不是块内容：分块内容不落库，仅在入库阶段用于统计与进度记录。
    """
    data = _post("/ingest", {"docs": [{"id": "_probe", "title": title, "content": content}]})
    if not data or not isinstance(data.get("results"), list) or not data["results"]:
        return None

    chunks = data["results"][0].get("chunks")
    if not isinstance(chunks, list):
        return None
    return len(chunks)


def _business_base_url() -> Optional[str]:
    url = (os.getenv(_BUSINESS_URL_ENV) or "").strip()
    if not url:
        return None
    return url.rstrip("/")


def business_enabled() -> bool:
    """是否配置了 Go 业务服务地址。"""
    return _business_base_url() is not None


def order_query(user_id: str) -> Optional[dict]:
    """调用 Go 业务服务查询主订单；未配置或不可用返回 None。

    返回结构与 Python 的 orders.query_order 完全一致，调用方无需区分来源。
    """
    base = _business_base_url()
    if base is None or not user_id:
        return None

    url = f"{base}/api/order?user_id={urllib.parse.quote(user_id)}"
    try:
        with _opener.open(url, timeout=_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def pending_tickets(
    user_id: Optional[str] = None,
    include_expired: bool = False,
    timeout_seconds: Optional[int] = None,
) -> Optional[dict]:
    """调用 Go 业务服务查询待确认工单；未配置或不可用返回 None。

    列表拉取是低频操作，走 HTTP 不构成热点压力，因此接入收益高于认证校验。
    返回体必须是带 items 列表的对象，结构不完整时同样按不可用处理——
    远程只是加速，不能把半截结果交给业务层。
    """
    base = _business_base_url()
    if base is None:
        return None

    params = {"include_expired": "true" if include_expired else "false"}
    if user_id:
        params["user_id"] = user_id
    if timeout_seconds is not None:
        params["timeout_seconds"] = str(timeout_seconds)

    url = f"{base}/api/tickets/pending?{urllib.parse.urlencode(params)}"
    try:
        with _opener.open(url, timeout=_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    return data


def rank(
    query: str,
    results: list[dict],
    *,
    top_k: int,
    threshold: Optional[float] = None,
    lambda_: Optional[float] = None,
    config: Optional[dict] = None,
) -> Optional[list[dict]]:
    """调用 Go 的 /rank 做检索后处理；未配置或不可用返回 None。

    与 /search 的分工：/search 负责「召回 + 排序」，/rank 负责模型之后的
    确定性处理——复合分、阈值降级、MMR 去冗余。重排模型仍留在 Python，
    因为它需要供应商 SDK 与密钥；这里只把已有召回结果交给 Go 加工。

    返回结构与入参保持同一套字段，调用方无需区分来源。
    """
    if not results:
        return None

    payload: dict = {"query": query, "top_k": top_k, "results": results}
    if threshold is not None:
        payload["threshold"] = threshold
    if lambda_ is not None:
        payload["lambda"] = lambda_
    if config is not None:
        payload["config"] = config

    data = _post("/rank", payload)
    if not data or not isinstance(data.get("results"), list):
        return None
    return data["results"]


def _business_post(path: str, payload: dict, timeout: float = _TIMEOUT_SECONDS) -> Optional[dict]:
    """向业务服务发一次 JSON POST，返回 ``{"status": 状态码, "body": 响应体}``。

    与只读接口的关键差异：**4xx 不能当成「服务不可用」直接降级**。
    「工单不存在」「流转非法」是 Go 给出的业务结论，必须把状态码和响应体
    一起交给调用方翻译成对应异常；只有连不上、超时、响应不是 JSON 才返回
    ``None`` 让调用方走本地实现。

    urllib 对 4xx/5xx 抛 HTTPError，而 HTTPError 本身就是可读的响应对象，
    响应体要在 except 分支里读出来——这是本函数与 ``_post`` 唯一的结构差异。
    """
    base = _business_base_url()
    if base is None:
        return None

    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {"status": response.status, "body": body if isinstance(body, dict) else {}}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return None
        return {"status": exc.code, "body": body if isinstance(body, dict) else {}}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def confirm_ticket(ticket_id: str, operator: str) -> Optional[dict]:
    """调用 Go 确认待确认草稿；未配置或不可用返回 None。

    返回 ``{"status": 状态码, "body": 工单或错误体}``，由调用方决定
    是取工单、抛业务异常，还是降级到本地实现。
    """
    if not ticket_id:
        return None
    return _business_post(
        "/api/tickets/confirm",
        {"ticket_id": ticket_id, "operator": operator},
    )


def cancel_ticket(ticket_id: str, reason: str, operator: str) -> Optional[dict]:
    """调用 Go 取消待确认草稿；未配置或不可用返回 None。"""
    if not ticket_id:
        return None
    return _business_post(
        "/api/tickets/cancel",
        {"ticket_id": ticket_id, "reason": reason, "operator": operator},
    )


def update_ticket_status(ticket_id: str, status: str) -> Optional[dict]:
    """调用 Go 改工单状态；未配置或不可用返回 None。

    不传操作人：Go 侧与 Python 原实现一致，把该动作的操作人固定为 system。
    """
    if not ticket_id:
        return None
    return _business_post(
        "/api/tickets/status",
        {"ticket_id": ticket_id, "status": status},
    )
