from fastapi import APIRouter, Header

from backend.database import SessionLocal
from backend.services import auth, orders, tickets
from backend.models import TriedStep
from backend.models.ticket import TicketTransitionError
from backend.schemas import CreateTicketRequest, ReplyRequest
from backend.schemas.ticket import TransferRequest

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
        pending=req.pending,
    )
    if created:
        message = "工单已挂起待确认" if req.pending else "工单已创建"
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
