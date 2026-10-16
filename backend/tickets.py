import hashlib
import time
import uuid

# 内存工单表（demo 阶段不引数据库）
# 一个工单对应一次「问题 → 排查 → 建单」的闭环
_TICKETS: dict[str, dict] = {}

# 工单状态机：open(待处理) -> in_progress(处理中) -> resolved(已解决)
# 人工回复会记录在 replies 里


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
) -> dict:
    key = _dedup_key(user_id, description)
    # 幂等：同一用户同一问题只建一张单，重复确认返回已有工单
    for t in _TICKETS.values():
        if t["dedup_key"] == key and t["status"] != "resolved":
            return t

    ticket = {
        "id": f"T-{1000 + len(_TICKETS) + 1}",
        "dedup_key": key,
        "user_id": user_id,
        "user_name": user_name,
        "description": description,
        "tried_steps": tried_steps,
        "evidence": evidence,
        "status": "open",  # open / in_progress / resolved
        "created_at": time.time(),
        "replies": [],
    }
    _TICKETS[ticket["id"]] = ticket
    return ticket


def list_tickets() -> list[dict]:
    return sorted(_TICKETS.values(), key=lambda t: t["created_at"], reverse=True)


def get_ticket(ticket_id: str) -> dict | None:
    return _TICKETS.get(ticket_id)


def add_reply(ticket_id: str, replier: str, content: str) -> dict | None:
    t = _TICKETS.get(ticket_id)
    if not t:
        return None
    t["replies"].append(
        {"replier": replier, "content": content, "at": time.time()}
    )
    if t["status"] == "open":
        t["status"] = "in_progress"
    return t


def update_status(ticket_id: str, status: str) -> dict | None:
    t = _TICKETS.get(ticket_id)
    if not t:
        return None
    t["status"] = status
    return t


def seed_tickets() -> None:
    """演示数据：预置两张工单，方便管理员端第一次打开就有内容。"""
    if _TICKETS:
        return
    create_ticket(
        user_id="u-customer",
        user_name="李女士",
        description="专业版导出功能一直失败",
        tried_steps="重启客户端 · 检查网络连接",
        evidence="产品手册《导出功能排查指南》",
    )
    create_ticket(
        user_id="u-customer",
        user_name="王先生",
        description="激活码输入提示无效",
        tried_steps="核对邮箱中的激活码",
        evidence="FAQ《激活码常见问题》",
    )
