from fastapi import APIRouter, Header
from pydantic import BaseModel

from backend import auth, tickets

tickets_router = APIRouter()


def _current_user(authorization: str):
    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


class CreateTicketRequest(BaseModel):
    description: str
    tried_steps: str = ""
    evidence: str = ""


class ReplyRequest(BaseModel):
    ticket_id: str
    content: str


@tickets_router.post("/tickets")
def create_ticket(req: CreateTicketRequest, authorization: str = Header(default="")):
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    ticket = tickets.create_ticket(
        user_id=user["id"],
        user_name=user["name"],
        description=req.description,
        tried_steps=req.tried_steps,
        evidence=req.evidence,
    )
    return {"ticket": ticket}


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
    t = tickets.update_status(body.get("ticket_id"), body.get("status"))
    return {"ticket": t}
