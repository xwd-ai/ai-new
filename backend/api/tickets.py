from fastapi import APIRouter, Header

from backend.database import SessionLocal
from backend.services import auth, orders, tickets
from backend.models import TriedStep
from backend.models.ticket import TicketTransitionError
from backend.schemas import CreateTicketRequest, ReplyRequest
from backend.schemas.ticket import CancelRequest, ConfirmRequest, TransferRequest

tickets_router = APIRouter()


def _current_user(authorization: str):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


@tickets_router.post("/tickets")
def create_ticket(req: CreateTicketRequest, authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}

    # 若前端未显式传已尝试步骤，则从会话已记录的步骤自动沉淀
    tried_steps = req.tried_steps
    if not tried_steps:
        db = SessionLocal()
        try:
            steps = (
                db.query(TriedStep)
                .filter(TriedStep.user_id == user["id"])
                .order_by(TriedStep.at.asc())
                .all()
            )
            tried_steps = " · ".join(s.step for s in steps) if steps else ""
        finally:
            db.close()

    ticket, created = tickets.create_ticket(
        user_id=user["id"],
        user_name=user["name"],
        description=req.description,
        tried_steps=tried_steps,
        evidence=req.evidence,
    )
    if created:
        message = "工单已生成待确认草稿，请确认后创建"
    else:
        message = "该问题已有未解决工单，已返回原工单"
    return {
        "ticket": ticket,
        "created": created,
        "message": message,
    }


@tickets_router.get("/tickets")
def list_tickets(authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    # 管理员看全部；普通用户只看自己的
    if user["role"] == "admin":
        items = tickets.list_tickets()
    else:
        items = [t for t in tickets.list_tickets() if t["user_id"] == user["id"]]
    return {"tickets": items}


@tickets_router.get("/order")
def my_order(authorization: str = Header(default="")):
    """查询当前用户订单与服务范围，供前端展示。"""
    user = _current_user(authorization)
    if not user:
        return {"order": None}
    return {"order": orders.query_order(user["id"])}


@tickets_router.post("/tickets/reply")
def reply_ticket(req: ReplyRequest, authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    if user["role"] != "admin":
        return {"error": "无权限"}
    t = tickets.add_reply(req.ticket_id, user["name"], req.content)
    return {"ticket": t}


@tickets_router.post("/tickets/status")
def update_status(body: dict, authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    if user["role"] != "admin":
        return {"error": "无权限"}
    try:
        t = tickets.update_status(body.get("ticket_id"), body.get("status"))
    except TicketTransitionError as e:
        return {"error": str(e)}
    return {"ticket": t}


@tickets_router.post("/tickets/transfer")
def transfer_ticket(req: TransferRequest, authorization: str = Header(default="")):
    """将工单转人工处理，记录转人工原因。

    仅管理员可转人工；非法来源状态（如已关闭、已转人工的工单）会被
    状态流转校验拒绝，返回明确错误信息。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    if user["role"] != "admin":
        return {"error": "无权限"}
    try:
        t = tickets.transfer_to_human(req.ticket_id, req.reason)
    except TicketTransitionError as e:
        return {"error": str(e)}
    if t is None:
        return {"error": "工单不存在"}
    return {"ticket": t}


@tickets_router.post("/tickets/confirm")
def confirm_ticket(req: ConfirmRequest, authorization: str = Header(default="")):
    """确认待确认草稿，将其正式流转为待处理工单。

    幂等：重复确认或确认已终结工单时原样返回，不产生副作用；
    工单不存在返回「工单不存在」，非法来源状态由流转校验拒绝。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    try:
        t = tickets.confirm_ticket(req.ticket_id)
    except TicketTransitionError as e:
        return {"error": str(e)}
    if t is None:
        return {"error": "工单不存在"}
    return {"ticket": t}


@tickets_router.post("/tickets/cancel")
def cancel_ticket(req: CancelRequest, authorization: str = Header(default="")):
    """取消待确认草稿，将其流转为已关闭，并记录取消原因。

    幂等：重复取消或取消已终结工单时原样返回，不产生副作用；
    工单不存在返回「工单不存在」，非法来源状态由流转校验拒绝。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    try:
        t = tickets.cancel_ticket(req.ticket_id, req.reason)
    except TicketTransitionError as e:
        return {"error": str(e)}
    if t is None:
        return {"error": "工单不存在"}
    return {"ticket": t}


@tickets_router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, authorization: str = Header(default="")):
    """查询单个工单详情，返回完整状态时间线（按时间升序）。

    管理员可查看任意工单；普通用户仅可查看本人创建的工单，
    越权访问返回「无权限」，工单不存在返回「工单不存在」。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    data = tickets.get_ticket(ticket_id)
    if data is None:
        return {"error": "工单不存在"}
    if user["role"] != "admin" and data["user_id"] != user["id"]:
        return {"error": "无权限"}
    return {"ticket": data}
