from fastapi import APIRouter, Header, Query

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
def list_tickets(
    status: list[str] | None = Query(default=None, description="按状态筛选，支持单状态、逗号分隔多状态与重复传参"),
    keyword: str | None = Query(default=None, description="关键词，模糊匹配工单号/问题描述/客户名"),
    q: str | None = Query(default=None, description="关键词别名，与 keyword 等价"),
    page: int | None = Query(default=None, description="页码，从 1 开始"),
    page_size: int | None = Query(default=None, description="每页条数，默认 20，上限 100"),
    order: str | None = Query(default=None, description="按建单时间排序，asc 正序 / desc 逆序（默认）"),
    authorization: str = Header(default=""),
):
    """分页查询工单列表，支持按状态、关键词筛选与按建单时间正逆序排序。

    status 支持 status=open、status=open,closed、status=open&status=closed
    三种写法；不传或传 all 表示不筛选。未知状态直接返回明确错误，不静默给空列表。
    keyword（或等价的 q）按工单号、问题描述、客户名做模糊匹配，任一命中即保留，
    可与状态筛选、分页、排序叠加使用。page / page_size 控制分页，order 控制时间正逆序；
    分页参数或关键词非法时返回明确错误。
    管理员看全部工单，普通用户只看本人工单；响应回显生效的筛选、分页与排序条件。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    # keyword 优先，未传时回退到别名 q，便于前端沿用搜索框参数名
    raw_keyword = keyword if keyword is not None else q
    try:
        statuses = tickets.normalize_status_filter(status)
        result = tickets.list_tickets(
            statuses=statuses,
            user_id=None if user["role"] == "admin" else user["id"],
            page=page,
            page_size=page_size,
            order=order,
            keyword=raw_keyword,
        )
    except ValueError as e:
        return {"error": str(e)}

    items = result["items"]
    conditions = []
    if statuses:
        conditions.append(f"状态 {'/'.join(statuses)}")
    if result["keyword"]:
        conditions.append(f"关键词「{result['keyword']}」")
    scope = f"按 {' + '.join(conditions)} 筛选到" if conditions else "共"
    if result["pages"] > 1:
        message = f"{scope} {result['total']} 个工单，当前第 {result['page']}/{result['pages']} 页"
    else:
        message = f"{scope} {result['total']} 个工单"
    return {
        "tickets": items,
        "total": result["total"],
        "page": result["page"],
        "page_size": result["page_size"],
        "pages": result["pages"],
        "has_more": result["has_more"],
        "order": result["order"],
        "statuses": statuses or [],
        "keyword": result["keyword"],
        "message": message,
    }


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


@tickets_router.post("/tickets/expire")
def expire_pending_tickets(
    timeout_seconds: int | None = None,
    limit: int | None = None,
    authorization: str = Header(default=""),
):
    """扫描并批量收敛超时未确认的待确认草稿。

    仅管理员可触发，供定时任务或运营手动调用：超过超时阈值仍未确认的草稿
    统一关闭，并写入取消说明与状态时间线。timeout_seconds / limit 可覆盖默认
    阈值与单批上限，返回的 remaining 大于 0 表示仍有超时草稿待下一批处理。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    if user["role"] != "admin":
        return {"error": "无权限"}
    result = tickets.expire_pending_tickets(timeout_seconds=timeout_seconds, limit=limit)
    if result["closed"]:
        message = f"已关闭 {result['closed']} 个超时待确认工单"
    else:
        message = "没有需要关闭的超时待确认工单"
    return {"result": result, "message": message}


@tickets_router.get("/tickets/pending")
def pending_tickets(
    include_expired: bool = False,
    timeout_seconds: int | None = None,
    all_users: bool = False,
    authorization: str = Header(default=""),
):
    """拉取待确认草稿列表，供前端展示确认卡片。

    默认只返回仍可确认的草稿；include_expired=true 时连同已超时但尚未收敛的
    草稿一起返回，每条附带 expires_at / remaining_seconds / expired，前端可据此
    展示确认倒计时。普通用户只能看到自己的草稿，管理员需要全量视图时传
    all_users=true。timeout_seconds 可覆盖默认超时阈值，用于联调验证。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    # 普通用户强制按本人隔离；管理员默认也只看自己的，显式要求时才看全量
    scope_user = None if (user["role"] == "admin" and all_users) else user["id"]
    result = tickets.list_pending_tickets(
        user_id=scope_user,
        include_expired=include_expired,
        timeout_seconds=timeout_seconds,
    )
    items = result["items"]
    if not items:
        message = "暂无待确认工单"
    elif result["expired"]:
        message = f"共 {result['total']} 个待确认工单，其中 {result['expired']} 个已超时"
    else:
        message = f"共 {result['total']} 个待确认工单"
    return {
        "tickets": items,
        "total": result["total"],
        "expired": result["expired"],
        "timeout_seconds": result["timeout_seconds"],
        "message": message,
    }


@tickets_router.get("/tickets/{ticket_id}/transfer-decision")
def transfer_decision(
    ticket_id: str,
    user_text: str = Query(default="", max_length=500, description="用户当前消息，用于识别主动要求转人工"),
    feature: str = Query(default="", max_length=50, description="问题涉及的功能，用于判断是否在当前版本能力内"),
    authorization: str = Header(default=""),
):
    """按规则判定工单是否需要转人工，返回命中原因与判定依据。

    三条规则按优先级返回：用户主动要求、超出服务范围（无订单 / 服务到期 /
    版本不含该功能）、多次排查无效（已尝试步骤或人工回复达到阈值）。
    判定只读：不改变工单状态，也不追加回复，调用方据此决定是否调用转人工接口。
    服务范围取自工单所属用户的订单而非当前登录账号，管理员查看他人工单时结论
    仍对应工单本人的服务范围。普通用户仅可判定本人工单，越权返回「无权限」。
    """
    user = _current_user(authorization)
    if not user:
        return {"error": "未登录"}
    data = tickets.get_ticket(ticket_id)
    if data is None:
        return {"error": "工单不存在"}
    if user["role"] != "admin" and data["user_id"] != user["id"]:
        return {"error": "无权限"}

    decision = tickets.evaluate_transfer_rule(
        data,
        order=orders.query_order(data["user_id"]),
        user_text=user_text,
        feature=feature,
    )
    return {"decision": decision, "message": decision["message"]}


def _detail_message(data: dict) -> str:
    """按详情各分区内容拼装一句话摘要，空分区给出「暂无」提示。

    摘要只依赖组装好的详情结构，不再单独回查数据：排查信息看是否有条目，
    回复看总数与人工/系统分布，前端与接口响应共用同一份统计结果。
    """
    troubleshooting = data["troubleshooting"]
    reply_timeline = data["reply_timeline"]
    parts = [f"工单 {data['id']}"]
    if troubleshooting["has_tried_steps"] or troubleshooting["has_evidence"]:
        parts.append(
            f"留存 {troubleshooting['tried_step_count']} 条已尝试步骤、"
            f"{troubleshooting['evidence_count']} 条排查依据"
        )
    else:
        parts.append("暂无已尝试步骤与排查依据")
    if reply_timeline["has_replies"]:
        parts.append(
            f"共 {reply_timeline['total']} 条回复（人工 {reply_timeline['human_count']} 条、"
            f"系统 {reply_timeline['system_count']} 条）"
        )
    else:
        parts.append("暂无回复记录")
    return "，".join(parts)


@tickets_router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, authorization: str = Header(default="")):
    """查询单个工单详情，返回结构化排查信息、状态时间线与回复时间线。

    troubleshooting 汇总已尝试步骤与排查依据：每个条目带自增序号，依据条目
    还会拆出来源字段；同时给出条目计数与是否有内容的标记，前端可直接逐条渲染。
    reply_timeline 逐条给出回复人、内容、时间与来源（人工/系统），并附人工回复数、
    系统说明数、首次人工响应耗时，取消原因与转人工说明也能在同一时间线里还原。
    detail_summary 汇总各分区的关键指标（状态链路、流转次数、回复计数等），
    供前端做一屏概览。原始 tried_steps / evidence / replies 字段照旧返回，
    保证既有展示不受影响。
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
    return {"ticket": data, "message": _detail_message(data)}
