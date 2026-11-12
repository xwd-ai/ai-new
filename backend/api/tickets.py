"""工单接口层：建单、列表、详情、状态流转、批量操作与统计。

约定：
- 权限在路由签名上声明：需要登录的接口挂 Depends(require_login)，管理员接口挂
  Depends(require_admin)，匿名可用的接口挂 Depends(current_user)；
  判定口径只有认证层一处，接口体内不再解析请求头、也不比对角色字符串；
- 认证层抛出的 401/403 由 TicketErrorRoute 统一转成工单模块的错误码，
  对外错误契约不因权限实现方式变化而改变；
- 单条数据的归属校验仍走 _require_ticket：它取数与鉴权合一，与登录态解析是两件事；
- 业务失败一律抛 TicketBusinessError 派生异常，由 TicketErrorRoute 统一转成
  {error, error_code, error_detail?} 响应，接口内不拼装错误字典；
- 纯拼接提示文案的辅助函数集中在文件顶部，路由按功能分组排列；
- 固定路径（/tickets/pending、/tickets/stats 等）必须定义在 /tickets/{ticket_id}
  之前，否则会被路径参数匹配吞掉。
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from backend.api.auth import current_user, require_admin, require_login
from backend.database import SessionLocal
from backend.models import TriedStep
from backend.models.order import OrderBusinessError
from backend.models.ticket import (
    TicketAuthRequiredError,
    TicketBusinessError,
    TicketNotFoundError,
    TicketPermissionError,
    TicketRuleError,
)
from backend.schemas import CreateTicketRequest, ReplyRequest
from backend.schemas.order import (
    AdminOrderListView,
    ExpireResultResponse,
    FeatureCheckResponse,
    OrderListView,
    OrderQueryRequest,
    OrderScopeResponse,
    RenewalListView,
)
from backend.schemas.ticket import (
    BatchCloseRequest,
    BatchTransferRequest,
    CancelRequest,
    ConfirmRequest,
    TransferRequest,
)
from backend.services import auth, orders, tickets


def _as_ticket_error(exc: auth.AuthError) -> Optional[TicketBusinessError]:
    """把认证层的权限异常翻译成工单模块的错误码，无法归类时返回 None。

    权限依赖由认证层实现，但工单接口对外的错误码属于本模块的契约：
    未登录一律 TICKET_UNAUTHORIZED、无权限一律 TICKET_FORBIDDEN，
    前端按错误码分支的逻辑不因为权限判定换了实现而失效。
    认证层将来新增的其它 AuthError 不在这里硬猜，交给上层按原样返回。
    """
    if isinstance(exc, auth.TokenError):
        return TicketAuthRequiredError()
    if isinstance(exc, auth.PermissionDeniedError):
        return TicketPermissionError()
    return None


class TicketErrorRoute(APIRoute):
    """把工单业务异常统一转换为错误响应。

    业务代码只负责抛出 TicketBusinessError 派生异常，本路由类在一个地方拼装响应：
    HTTP 状态取自异常的 http_status，响应体固定为
    {"error": 文案, "error_code": 错误码, "error_detail": 补充上下文（可选）}。
    新增接口不必再各写一遍 try/except，错误结构也不会随接口各自漂移。
    权限依赖抛出的认证异常也在这里收口，路由签名只声明「需要什么身份」，
    不必为了对齐错误码在每个接口里再包一层转换。订单接口挂在本路由下，
    订单业务异常沿用同一套响应结构，避免同一模块里出现两种错误契约。
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def dispatch(request: Request):
            try:
                return await handler(request)
            except (TicketBusinessError, OrderBusinessError) as exc:
                return JSONResponse(status_code=exc.http_status, content=exc.to_payload())
            except auth.AuthError as exc:
                translated = _as_ticket_error(exc)
                if translated is None:
                    raise
                return JSONResponse(
                    status_code=translated.http_status, content=translated.to_payload()
                )

        return dispatch


tickets_router = APIRouter(route_class=TicketErrorRoute)


# ---------------------------------------------------------------------------
# 辅助函数：取单鉴权与响应摘要（纯函数，不直接读写数据库）
# ---------------------------------------------------------------------------


def _require_ticket(user: dict, ticket_id: str, action: str = "查看") -> dict:
    """取工单并校验数据权限：不存在报 404，非本人且非管理员报 403。

    取数与鉴权合成一步，各接口不必先查详情再自己比对 user_id；
    动作名只用于错误详情，便于提示「查看/确认/取消/判定」是哪一步被拦。
    登录态已由权限依赖解析好，这里只管「这个人能不能动这张单」。
    """
    data = tickets.get_ticket(ticket_id)
    if data is None:
        raise TicketNotFoundError(ticket_id)
    tickets.ensure_ticket_access(user, data, action)
    return data


def _batch_message(action: str, result: dict) -> str:
    """按批量结果拼装一句话摘要，失败时附带首个失败原因。"""
    message = f"批量{action}完成：成功 {result['succeeded']} 个，失败 {result['failed']} 个"
    if result["failures"]:
        first = result["failures"][0]
        message += f"（{first['ticket_id']}：{first['reason']}）"
    return message


def _stats_message(stats: dict) -> str:
    """按统计结果拼装一句话摘要：总数、各状态分布与平均处理时长。

    只列出计数不为 0 的状态，空状态不占篇幅；没有结案工单时明确说明
    「暂无处理时长样本」，而不是给出一个 0 秒的平均值。
    """
    parts = [
        f"{item['label']} {item['count']} 个"
        for item in stats["status_breakdown"]
        if item["count"]
    ]
    message = f"共 {stats['total']} 个工单"
    if parts:
        message += "：" + "、".join(parts)
    duration = stats["handling_duration"]
    if duration["sample_size"]:
        message += (
            f"；平均处理时长 {duration['avg_text']}"
            f"（样本 {duration['sample_size']} 个，闭环率 {stats['settled']['completion_rate']}%）"
        )
    else:
        message += "；暂无已结案工单可统计处理时长"
    if stats["range"]["days"]:
        message += f"（统计最近 {stats['range']['days']} 天创建）"
    return message


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


# ---------------------------------------------------------------------------
# 建单
# ---------------------------------------------------------------------------


@tickets_router.post("/tickets")
def create_ticket(req: CreateTicketRequest, user: dict = Depends(require_login)):
    """创建待确认草稿，工单归属取自登录身份。"""
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


# ---------------------------------------------------------------------------
# 列表与订单
# ---------------------------------------------------------------------------


@tickets_router.get("/tickets")
def list_tickets(
    status: list[str] | None = Query(default=None, description="按状态筛选，支持单状态、逗号分隔多状态与重复传参"),
    keyword: str | None = Query(default=None, description="关键词，模糊匹配工单号/问题描述/客户名"),
    q: str | None = Query(default=None, description="关键词别名，与 keyword 等价"),
    page: int | None = Query(default=None, description="页码，从 1 开始"),
    page_size: int | None = Query(default=None, description="每页条数，默认 20，上限 100"),
    order: str | None = Query(default=None, description="按建单时间排序，asc 正序 / desc 逆序（默认）"),
    user: dict = Depends(require_login),
):
    """分页查询工单列表，支持按状态、关键词筛选与按建单时间正逆序排序。

    status 支持 status=open、status=open,closed、status=open&status=closed
    三种写法；不传或传 all 表示不筛选。未知状态直接返回明确错误，不静默给空列表。
    keyword（或等价的 q）按工单号、问题描述、客户名做模糊匹配，任一命中即保留，
    可与状态筛选、分页、排序叠加使用。page / page_size 控制分页，order 控制时间正逆序；
    分页参数或关键词非法时返回明确错误。
    管理员看全部工单，普通用户只看本人工单；响应回显生效的筛选、分页与排序条件。
    """
    # keyword 优先，未传时回退到别名 q，便于前端沿用搜索框参数名
    raw_keyword = keyword if keyword is not None else q
    statuses = tickets.normalize_status_filter(status)
    result = tickets.list_tickets(
        statuses=statuses,
        user_id=tickets.user_scope(user),
        page=page,
        page_size=page_size,
        order=order,
        keyword=raw_keyword,
    )

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
def my_order(user: Optional[dict] = Depends(current_user)):
    """查询当前用户订单与服务范围，供前端展示。

    匿名访问不报错，返回空订单：前端据此引导登录，
    而不是把「还没登录」处理成一次接口异常。
    """
    if not user:
        return {"order": None}
    return {"order": orders.query_order(user["id"])}


@tickets_router.get("/order/list", response_model=OrderListView)
def my_orders(user: dict = Depends(require_login)):
    """列出当前用户的全部订单，并标出主订单与在期能力并集。

    与 /order 的分工：/order 只给一条主订单，供右侧面板展示当前版本；
    本接口面向多产品用户——买了基础版又买了专业版时，
    需要看到每一条订单各自的服务期与状态，而不是只看到其中一条。

    匿名访问在这里直接拒绝，不做「返回空列表」的宽松处理：
    空列表与「确实没有订单」无法区分，调用方容易把未登录当成没买过东西。
    """
    return orders.query_orders(user["id"])


@tickets_router.get("/order/scope", response_model=OrderScopeResponse)
def order_scope(
    params: Annotated[OrderQueryRequest, Query()],
    user: dict = Depends(require_login),
):
    """查询当前用户订单的服务范围：版本、服务期与功能是否包含。

    与 /order 的分工：/order 面向页面展示、字段固定；本接口回答「能不能用」，
    把判定结论、原因码与一句可展示的说明一次给全，供前端提示与对话链路引用。
    与 /order 的匿名宽松策略不同，这里要求登录：结论涉及账号权益，
    未登录应当被明确拒绝，而不是返回一份空结论让调用方误当成「没有订单」。

    feature 走查询参数（?feature=批量导出），留空只判断整体服务状态；
    order_id 同样走查询参数，指定后只判定那一条订单，留空判定主订单。
    功能名解析与判定全部在服务层完成，接口层不重复解释规则。
    """
    return orders.describe_order(
        user["id"], feature=params.feature, order_id=params.order_id or None
    )


@tickets_router.get("/order/feature-scope", response_model=FeatureCheckResponse)
def order_feature_scope(
    feature: str = Query(
        default="", max_length=50, description="要确认的功能，如「批量导出」「API」"
    ),
    user: dict = Depends(require_login),
):
    """判定某功能是否在当前用户的服务范围内（跨全部订单）。

    与 /order/scope 的分工：那个接口判定单条订单，多产品用户可能被主订单
    带偏；本接口把全部订单放在一起看，任一在期订单的版本覆盖该功能即判为
    在范围内，回答「这个功能现在能不能用」，供前端提示与对话链路引用。

    feature 走查询参数，可以是标准名称、能力编码或用户口语；
    解析与判定全部在服务层完成，接口层只做取数透传与登录校验。
    """
    return orders.describe_feature_scope(user["id"], feature=feature)


@tickets_router.get("/order/renewal", response_model=RenewalListView)
def order_renewal(
    window_days: int | None = Query(
        default=None, ge=0, le=365, description="提前多少天开始提示续费，默认 30"
    ),
    user: dict = Depends(require_login),
):
    """列出当前用户各订单的续费提醒。

    与 /order/scope 的分工：那个接口回答「现在能不能用」，本接口回答
    「什么时候该续费」——服务期换算成提醒状态、级别与建议动作，
    前端据此决定是弹窗、列表标记，还是什么都不做。

    已退订订单不提醒续费，只提示重新购买；服务期还没开始的订单也不提醒，
    提前催一个用都用不上的服务只会让人以为系统算错了时间。
    """
    return orders.list_renewal_hints(user["id"], window_days=window_days)


@tickets_router.get("/order/admin", response_model=AdminOrderListView)
def admin_orders(
    status: list[str] | None = Query(
        default=None,
        description="按订单状态筛选，支持单状态、逗号分隔多状态与重复传参，传 all 不筛选",
    ),
    tier: list[str] | None = Query(
        default=None,
        description="按产品版本筛选，支持 basic / pro 及中文别名，可多选",
    ),
    user_id: str | None = Query(default=None, description="按客户编号精确筛选"),
    keyword: str | None = Query(default=None, description="关键词，模糊匹配订单号/客户编号"),
    page: int | None = Query(default=None, ge=1, description="页码，从 1 开始"),
    page_size: int | None = Query(default=None, ge=1, le=100, description="每页条数，默认 20，上限 100"),
    order: str | None = Query(default=None, description="按到期时间排序，asc 正序 / desc 逆序（默认）"),
    user: dict = Depends(require_admin),
):
    """管理侧订单列表：跨客户查询订单，支持按状态、版本、客户与关键词筛选并分页。

    与 /order/list 的分工：那个接口只返回当前登录用户自己的订单，供页面展示；
    本接口面向客服，用来「按状态找到期订单」「按客户查历史购买」
    「按版本核对权益范围」，因此要求管理员身份。

    status / tier 都支持单值、逗号分隔与重复传参三种写法，不传或传 all 表示不筛选；
    未知状态或未知版本直接返回 400，不静默给空列表——空列表说明不了是「真没有」
    还是「条件写错了」。keyword（订单号或客户编号）与各筛选项之间是「与」关系。
    响应附带 by_status / by_tier 分组计数，覆盖全部命中筛选的订单而不只是当前页，
    翻页时分组标签不会跟着变。排序按到期时间，订单号作为次序兜底，
    保证同一到期时间的记录翻页时不重不漏。
    """
    return orders.list_all_orders(
        statuses=status,
        tiers=tier,
        user_id=user_id,
        keyword=keyword,
        page=page,
        page_size=page_size,
        order=order,
    )


@tickets_router.post("/order/expire", response_model=ExpireResultResponse)
def expire_orders(
    limit: int = Query(default=100, ge=1, le=500, description="单次最多归档的订单数"),
    user: dict = Depends(require_admin),
):
    """把服务期已结束、状态仍是有效的订单归档为过期。

    管理员触发的批量收敛：候选订单按到期时间从早到晚处理，每条都走订单状态机，
    因此退订订单与已过期订单不会被重复改动。单次处理条数有上限，
    返回 has_more 供调用方决定是否继续，避免一次请求跑太久。
    """
    return orders.expire_overdue_orders(limit=limit)


# ---------------------------------------------------------------------------
# 写操作：回复、改状态、转人工（均需管理员身份）
# ---------------------------------------------------------------------------


@tickets_router.post("/tickets/reply")
def reply_ticket(req: ReplyRequest, user: dict = Depends(require_admin)):
    """追加一条人工回复，待处理工单随回复推进到处理中。"""
    t = tickets.add_reply(req.ticket_id, user["name"], req.content)
    if t is None:
        raise TicketNotFoundError(req.ticket_id)
    return {"ticket": t}


@tickets_router.post("/tickets/status")
def update_status(body: dict, user: dict = Depends(require_admin)):
    """按目标状态更新工单，非法流转由状态校验拒绝。"""
    ticket_id = body.get("ticket_id")
    t = tickets.update_status(ticket_id, body.get("status"))
    if t is None:
        raise TicketNotFoundError(ticket_id)
    return {"ticket": t}


@tickets_router.post("/tickets/transfer")
def transfer_ticket(req: TransferRequest, user: dict = Depends(require_admin)):
    """将工单转人工处理，写入转人工原因与命中规则。

    转人工前先按规则判定一次：原因未填时用判定结果回填，保证每次转人工都有
    可追溯的原因；auto=true 表示完全按规则执行，未命中转人工规则则拒绝，
    不会把仍在服务范围内、排查轮次也足够的工单误转。auto=false 时管理员可
    显式指定原因直接转人工，判定结果仅作为响应里的参考依据返回。
    服务范围取自工单所属用户的订单，管理员处理他人工单时结论对应工单本人。
    仅管理员可转人工；非法来源状态（如已关闭的工单）由状态流转校验拒绝。
    """
    data = _require_ticket(user, req.ticket_id, "转人工")
    decision = tickets.evaluate_transfer_rule(
        data,
        order=orders.query_order(data["user_id"]),
        user_text=req.user_text,
        feature=req.feature,
    )
    if req.auto and not decision["should_transfer"]:
        raise TicketRuleError(
            f"未命中转人工规则：{decision['message']}",
            details={"decision": decision},
        )

    t = tickets.transfer_to_human(
        req.ticket_id,
        req.reason,
        operator=user["name"],
        decision=decision,
    )
    if t is None:
        raise TicketNotFoundError(req.ticket_id)

    transfer = t["transfer"]
    if transfer["reason_codes"]:
        codes = "/".join(transfer["reason_codes"])
        message = f"工单 {t['id']} 已转人工（命中规则：{codes}），原因：{transfer['reason']}"
    else:
        message = f"工单 {t['id']} 已转人工，原因：{transfer['reason']}"
    return {"ticket": t, "decision": decision, "message": message}


# ---------------------------------------------------------------------------
# 批量操作：批量关闭与批量转人工（均需管理员身份）
# ---------------------------------------------------------------------------


@tickets_router.post("/tickets/batch-close")
def batch_close_tickets(req: BatchCloseRequest, user: dict = Depends(require_admin)):
    """批量关闭工单，逐单落痕并返回成功与失败明细。

    仅管理员可调用：一次提交多个工单号，每张工单在同一批次内独立结算，
    个别工单不存在或当前状态不允许关闭时记入 failures 并给出错误码，
    不影响同批其他工单；关闭原因写入每张工单的回复时间线。
    单次最多 50 个工单，重复工单号只处理一次，空列表直接拒绝。
    """
    result = tickets.batch_close_tickets(
        req.ticket_ids, reason=req.reason, operator=user["name"]
    )
    return {"result": result, "message": _batch_message("关闭", result)}


@tickets_router.post("/tickets/batch-transfer")
def batch_transfer_tickets(req: BatchTransferRequest, user: dict = Depends(require_admin)):
    """批量转人工，转人工原因按转人工说明写入每张工单的时间线。

    仅管理员可调用：已终结或当前状态不允许转人工的工单进入 failures，
    其余工单照常流转，调用方据 success_ids 与 failures 决定是否需要重试。
    原因为空时以默认说明兜底，保证每条转人工记录都可追溯。
    """
    result = tickets.batch_transfer_tickets(
        req.ticket_ids, reason=req.reason, operator=user["name"]
    )
    return {"result": result, "message": _batch_message("转人工", result)}


# ---------------------------------------------------------------------------
# 两阶段建单：确认、取消与超时收敛
# ---------------------------------------------------------------------------


@tickets_router.post("/tickets/confirm")
def confirm_ticket(req: ConfirmRequest, user: dict = Depends(require_login)):
    """确认待确认草稿，将其正式流转为待处理工单。

    幂等：重复确认或确认已终结工单时原样返回，不产生副作用；
    工单不存在返回「工单不存在」，非法来源状态由流转校验拒绝。
    仅工单本人或管理员可确认，他人工单返回「无权限」。
    """
    _require_ticket(user, req.ticket_id, "确认")
    t = tickets.confirm_ticket(req.ticket_id)
    if t is None:
        raise TicketNotFoundError(req.ticket_id)
    return {"ticket": t}


@tickets_router.post("/tickets/cancel")
def cancel_ticket(req: CancelRequest, user: dict = Depends(require_login)):
    """取消待确认草稿，将其流转为已关闭，并记录取消原因。

    幂等：重复取消或取消已终结工单时原样返回，不产生副作用；
    工单不存在返回「工单不存在」，非法来源状态由流转校验拒绝。
    仅工单本人或管理员可取消，他人工单返回「无权限」。
    """
    _require_ticket(user, req.ticket_id, "取消")
    t = tickets.cancel_ticket(req.ticket_id, req.reason)
    if t is None:
        raise TicketNotFoundError(req.ticket_id)
    return {"ticket": t}


@tickets_router.post("/tickets/expire")
def expire_pending_tickets(
    timeout_seconds: int | None = None,
    limit: int | None = None,
    user: dict = Depends(require_admin),
):
    """扫描并批量收敛超时未确认的待确认草稿。

    仅管理员可触发，供定时任务或运营手动调用：超过超时阈值仍未确认的草稿
    统一关闭，并写入取消说明与状态时间线。timeout_seconds / limit 可覆盖默认
    阈值与单批上限，返回的 remaining 大于 0 表示仍有超时草稿待下一批处理。
    """
    result = tickets.expire_pending_tickets(timeout_seconds=timeout_seconds, limit=limit)
    if result["closed"]:
        message = f"已关闭 {result['closed']} 个超时待确认工单"
    else:
        message = "没有需要关闭的超时待确认工单"
    return {"result": result, "message": message}


# ---------------------------------------------------------------------------
# 待确认列表与统计：固定路径，必须定义在 /tickets/{ticket_id} 之前
# ---------------------------------------------------------------------------


@tickets_router.get("/tickets/pending")
def pending_tickets(
    include_expired: bool = False,
    timeout_seconds: int | None = None,
    all_users: bool = False,
    user: dict = Depends(require_login),
):
    """拉取待确认草稿列表，供前端展示确认卡片。

    默认只返回仍可确认的草稿；include_expired=true 时连同已超时但尚未收敛的
    草稿一起返回，每条附带 expires_at / remaining_seconds / expired，前端可据此
    展示确认倒计时。普通用户只能看到自己的草稿，管理员需要全量视图时传
    all_users=true。timeout_seconds 可覆盖默认超时阈值，用于联调验证。
    """
    # 普通用户强制按本人隔离；管理员默认也只看自己的，显式要求时才看全量
    scope_user = tickets.user_scope(user, include_admin_all=all_users)
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


@tickets_router.get("/tickets/stats")
def ticket_statistics(
    days: int | None = Query(default=None, description="只统计最近 N 天创建的工单，取值 1–365"),
    user_id: str | None = Query(default=None, description="按客户过滤，仅管理员可用"),
    user: dict = Depends(require_login),
):
    """工单统计：按状态计数与平均处理时长。

    返回各状态的计数与占比，并把状态分成「仍需推进」与「已结案」两组给出闭环率；
    处理时长按「建单 → 最后一次进入终态」计算，只统计已解决 / 已关闭的工单，
    同时给出平均、最短、最长、中位数与按状态拆分的结果，样本为空时指标为 null。
    days 只统计最近 N 天创建的工单，用于看近期趋势；越界或非整数直接拒绝。
    普通用户只能统计本人工单，传他人工号返回「无权限」；管理员不传 user_id
    统计全量，传了就只看该客户。
    """
    if tickets.is_admin(user):
        scope_user = user_id
    elif user_id and user_id != user["id"]:
        raise TicketPermissionError("普通用户只能查看本人工单统计")
    else:
        scope_user = user["id"]

    stats = tickets.build_ticket_statistics(user_id=scope_user, days=days)
    return {"stats": stats, "message": _stats_message(stats)}


# ---------------------------------------------------------------------------
# 转人工判定与工单详情
# ---------------------------------------------------------------------------


@tickets_router.get("/tickets/{ticket_id}/transfer-decision")
def transfer_decision(
    ticket_id: str,
    user_text: str = Query(default="", max_length=500, description="用户当前消息，用于识别主动要求转人工"),
    feature: str = Query(default="", max_length=50, description="问题涉及的功能，用于判断是否在当前版本能力内"),
    user: dict = Depends(require_login),
):
    """按规则判定工单是否需要转人工，返回命中原因与判定依据。

    三条规则按优先级返回：用户主动要求、超出服务范围（无订单 / 服务到期 /
    版本不含该功能）、多次排查无效（已尝试步骤或人工回复达到阈值）。
    判定只读：不改变工单状态，也不追加回复，调用方据此决定是否调用转人工接口。
    服务范围取自工单所属用户的订单而非当前登录账号，管理员查看他人工单时结论
    仍对应工单本人的服务范围。普通用户仅可判定本人工单，越权返回「无权限」。
    """
    data = _require_ticket(user, ticket_id, "判定")

    decision = tickets.evaluate_transfer_rule(
        data,
        order=orders.query_order(data["user_id"]),
        user_text=user_text,
        feature=feature,
    )
    return {"decision": decision, "message": decision["message"]}


@tickets_router.get("/tickets/{ticket_id}")
def ticket_detail(ticket_id: str, user: dict = Depends(require_login)):
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
    data = _require_ticket(user, ticket_id, "查看")
    return {"ticket": data, "message": _detail_message(data)}
