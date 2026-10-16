import hashlib
import time

from backend import store

# 工单持久化到 data.json（demo 阶段不引数据库）
# 一个工单对应一次「问题 → 排查 → 建单」的闭环
# 工单状态机：open(待处理) -> in_progress(处理中) -> resolved(已解决)


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


def _all_tickets() -> dict:
    return store.read()["tickets"]


def _save_tickets(tickets: dict) -> None:
    data = store.read()
    data["tickets"] = tickets
    store.write(data)


def _next_id(data: dict) -> str:
    data["ticket_seq"] += 1
    return f"T-{data['ticket_seq']}"


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
) -> dict:
    key = _dedup_key(user_id, description)
    data = store.read()
    tickets = data["tickets"]

    # 幂等：同一用户同一问题只建一张单，重复确认返回已有工单
    for t in tickets.values():
        if t["dedup_key"] == key and t["status"] != "resolved":
            return t

    ticket_id = _next_id(data)
    ticket = {
        "id": ticket_id,
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
    tickets[ticket_id] = ticket
    store.write(data)
    return ticket


def list_tickets() -> list[dict]:
    tickets = _all_tickets()
    return sorted(tickets.values(), key=lambda t: t["created_at"], reverse=True)


def get_ticket(ticket_id: str) -> dict | None:
    return _all_tickets().get(ticket_id)


def add_reply(ticket_id: str, replier: str, content: str) -> dict | None:
    data = store.read()
    t = data["tickets"].get(ticket_id)
    if not t:
        return None
    t["replies"].append(
        {"replier": replier, "content": content, "at": time.time()}
    )
    if t["status"] == "open":
        t["status"] = "in_progress"
    store.write(data)
    return t


def update_status(ticket_id: str, status: str) -> dict | None:
    data = store.read()
    t = data["tickets"].get(ticket_id)
    if not t:
        return None
    t["status"] = status
    store.write(data)
    return t


def seed_tickets() -> None:
    """演示数据：预置两张工单，方便管理员端第一次打开就有内容。"""
    if _all_tickets():
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
