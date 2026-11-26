"""工单服务层：建单、状态流转、列表查询、批量操作与转人工判定。

分层约定（文件内按此顺序分段）：
- 会话与取数：统一的连接边界与单条取数；
- 权限层：数据可见范围（普通用户仅本人，管理员可全量），角色判定复用认证层的口径；
- 认证联动层：把登录身份换算成工单侧的操作人，写接口统一从这里取身份与落痕名；
- 状态机层：状态变更的唯一写入口，负责校验、留痕与字段赋值；
- 入参归一化层：状态、分页、排序、关键词等外部入参收敛为内部口径；
- 写入层、查询层、统计层、组装层、判定层：各自只负责一件事。

两条贯穿全文件的规则：
- 状态值统一取自 TicketStatus 常量，业务代码里不出现裸状态字符串；
- 可预期失败以 TicketBusinessError 派生异常抛出（错误码见 models/ticket），
  由接口层在同一处转成 {error, error_code, error_detail?} 结构。
"""

import hashlib
import re
import time
from contextlib import contextmanager
from datetime import datetime
from typing import NamedTuple, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError

from backend.database import SessionLocal
from backend.models import Ticket
from backend.models.ticket import (
    TicketAuthRequiredError,
    TicketErrorCode,
    TicketPermissionError,
    TicketStatus,
    TicketStatusLog,
    TicketTransitionError,
    TicketValidationError,
    can_transition,
    is_valid_status,
    validate_transition,
)
from backend.services import auth as auth_service, go_client
from backend.tests.reference_api.distlock import distributed_lock
from backend.services.ticket_number import allocate_ticket_id, lock_mysql_ticket_counter

# 状态取值别名：业务代码统一引用这些常量，避免裸字符串分散在各处函数里
STATUS_PENDING_CONFIRM = TicketStatus.PENDING_CONFIRM.value
STATUS_OPEN = TicketStatus.OPEN.value
STATUS_IN_PROGRESS = TicketStatus.IN_PROGRESS.value
STATUS_RESOLVED = TicketStatus.RESOLVED.value
STATUS_TRANSFERRED_HUMAN = TicketStatus.TRANSFERRED_HUMAN.value
STATUS_CLOSED = TicketStatus.CLOSED.value

# 建单落地的初始状态：两阶段建单先挂起为待确认草稿，确认后才进入待处理
INITIAL_STATUS = STATUS_PENDING_CONFIRM

# 待确认草稿的超时阈值（秒）：超时未确认的草稿收敛为已关闭，避免僵尸工单堆积
PENDING_CONFIRM_TIMEOUT_SECONDS = 30 * 60
# 单次扫描最多收敛的工单数，分批消化积压，避免一次扫描长时间占用写锁
EXPIRE_BATCH_LIMIT = 200
# 单批收敛条数的硬上限：与订单归档接口保持同一口径，超过即视为入参错误
EXPIRE_BATCH_MAX = 500
# 超时收敛的写锁键：同一时刻只允许一个扫描任务在临界区内收敛
_EXPIRE_LOCK_KEY = "ticket:pending_confirm:expire"

# 列表分页默认值与单页上限：默认每页 20 条，上限 100 条，避免一次拉全表
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# 列表关键词长度上限：超长输入直接拒绝，避免构造超长匹配模式拖慢查询
MAX_KEYWORD_LENGTH = 50
# LIKE 转义符：关键词里的 % 与 _ 按字面量匹配，不当通配符使用
_LIKE_ESCAPE = "\\"

# 批量操作的工单数上限：单次请求最多处理该数量，避免一个请求长时间占用写锁
MAX_BATCH_SIZE = 50

# 统计区间的回溯上限（天）：超过一年的统计请求直接拒绝，避免一次扫过全量历史
MAX_STATS_DAYS = 365
# 视为处理已结束的终态：时间线进入这些状态的时刻即该工单的处理结束时间
SETTLED_STATUSES = (STATUS_RESOLVED, STATUS_CLOSED)
# 仍需推进的未闭环状态：待处理、处理中、待确认、已转人工
ACTIVE_STATUSES = (
    STATUS_OPEN,
    STATUS_IN_PROGRESS,
    STATUS_PENDING_CONFIRM,
    STATUS_TRANSFERRED_HUMAN,
)
# 状态的中文名：统计响应与摘要文案共用，避免同一状态在不同接口出现不同叫法
STATUS_LABELS = {
    STATUS_OPEN: "待处理",
    STATUS_IN_PROGRESS: "处理中",
    STATUS_RESOLVED: "已解决",
    STATUS_PENDING_CONFIRM: "待确认",
    STATUS_TRANSFERRED_HUMAN: "已转人工",
    STATUS_CLOSED: "已关闭",
}

# 排查信息结构化的上限：条目总数与单条长度双重限制，避免详情响应被超长文本撑爆
MAX_TROUBLESHOOT_ITEMS = 50
MAX_TROUBLESHOOT_ITEM_LENGTH = 200
# 条目分隔符：换行、中英文分号、间隔点（建单时多步合并写入的写法）、短横线列表
_ITEM_SEPARATOR_RE = re.compile(r"[\r\n]+|[;；]+|\s*[·•]\s*|\s+-\s+")
# 条目自带的序号前缀：1. / 1) / （1）/ ① / - / *，解析时剥离，序号由服务重新编号
_ITEM_PREFIX_RE = re.compile(r"^\s*(?:[（(]?\d{1,2}[)）.、]|[①-⑳]|[-*•])\s*")
# 正文中的连续编号：每个编号都是一个条目的起点，用于把粘连的列表还原成多条
_NUMBER_PREFIX_RE = re.compile(r"[（(]?\d{1,2}[)）.、]|[①-⑳]")
# 依据条目内的来源标注：「…（来源：产品手册第 3 节）」，剥离后作为独立字段
_SOURCE_RE = re.compile(r"[（(【\[]\s*(?:来源|出处|依据)\s*[:：]?\s*(?P<source>[^）)】\]]+?)\s*[）)】\]]")

# 详情回复时间线的条数上限：超出后只保留最近若干条，避免长会话把详情响应撑大
MAX_REPLY_ITEMS = 100
# 系统来源标记：该回复人不计为人工回复，仅用于取消、超时、转人工等系统说明
_SYSTEM_REPLIER = "system"
# 回复字段的兼容别名：历史数据里回复人/内容/时间可能落在不同键名下
_REPLIER_KEYS = ("replier", "author", "operator", "user_name", "name")
_CONTENT_KEYS = ("content", "text", "message")
_TIME_KEYS = ("at", "time", "created_at", "timestamp")

# 转人工阈值：已尝试步骤达到该条数仍无结论，视为多次排查无效
TRANSFER_TRIED_STEP_THRESHOLD = 3
# 转人工阈值：人工回复达到该轮数问题仍未闭环，视为反复沟通无效
TRANSFER_HUMAN_REPLY_THRESHOLD = 2
# 用户主动要求转人工的说法，命中任一即视为明确诉求
_TRANSFER_REQUEST_RE = re.compile(
    r"转人工|人工客服|人工服务|人工处理|真人客服|找人工|找客服|联系客服|升级处理",
    re.IGNORECASE,
)
# 判定原因码与展示文案，字典顺序即响应中的输出顺序（用户诉求优先）
TRANSFER_REASON_LABELS = {
    "user_requested": "用户主动要求转人工",
    "out_of_scope": "超出服务范围",
    "scope_unverified": "服务范围待人工核实",
    "ineffective_retries": "多次排查无效",
}
# 超出服务范围的三种细分情形，用于回显具体判定依据
SCOPE_REASON_TEXT = {
    "no_order": "未查询到有效订单",
    "service_expired": "服务已到期或订单非有效状态",
    "feature_not_included": "不包含该功能",
}
# 订单侧「判不了」的原因码：所购版本或功能名不可识别时，只能确认暂时无法
# 核实，并不等于用户超出服务范围。这类结论单独给码，不混进 out_of_scope，
# 避免把「识别不出功能名」记成「没买这项服务」。
UNVERIFIED_SCOPE_CODES = frozenset({"tier_unknown", "period_unknown", "feature_unknown"})
# 转人工未给出原因、规则也未命中时的默认说明，保证每次转人工都可追溯
DEFAULT_TRANSFER_REASON = "客服手动转人工"
# 转人工说明的回复类型标记：时间线据此与普通回复区分，便于单独渲染
_TRANSFER_KIND = "transfer"


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


@contextmanager
def advisory_lock(key: str):
    """建单临界区：三层并发控制，按可用能力逐级降级。

    1. **Redis 分布式锁**：多实例部署时跨进程互斥；缓存不可用则跳过；
    2. **SQLite**：单写者模型，用 BEGIN IMMEDIATE 取得库级写锁
       （SQLite 既没有行级锁，也没有 advisory lock）；
    3. **MySQL / PostgreSQL**：不加数据库锁，由 dedup_key 唯一约束兜底。

    第 1 层只减少并发冲突，第 3 层才是最终保证 —— 因此任何一层缺失都不会
    导致重复建单，只会多一些无谓的并发尝试。

    刻意不使用 MySQL 的 GET_LOCK：它是连接级命名锁，而连接在事务提交后会
    归还连接池，释放动作可能落到另一条连接上，导致锁滞留到连接断开。
    """
    with distributed_lock(f"lock:ticket:{key[:40]}"):
        db = SessionLocal()
        try:
            if db.get_bind().dialect.name == "sqlite":
                # 立即获取写锁，其他会话的写操作在此阻塞直到本事务提交/回滚
                db.execute(text("BEGIN IMMEDIATE"))
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


# ---------------------------------------------------------------------------
# 会话与取数：读写路径共用的连接边界与单条取数
# ---------------------------------------------------------------------------


@contextmanager
def _ticket_session(write: bool = False):
    """工单会话的统一边界：写入正常结束提交、异常回滚，只读直接关闭连接。

    各业务函数不再各自维护 try/finally，避免漏提交、漏回滚或漏关闭；
    只读路径不提交，取得的数据随会话关闭释放。
    """
    db = SessionLocal()
    try:
        yield db
        if write:
            db.commit()
    except Exception:
        if write:
            db.rollback()
        raise
    finally:
        db.close()


def _load_ticket(db, ticket_id: str, *, for_update=False):
    """按工单号取单条记录，不存在返回 None。

    工单号不是字符串时直接按「查不到」返回：请求体里混进对象、数组这类值
    不该一路走到 SQL 绑定层把接口打成 500，统一按不存在处理，
    所有取数入口（详情、回复、改状态、确认、取消）共享这一道防线。
    """
    if not isinstance(ticket_id, str):
        return None
    query = db.query(Ticket).filter(Ticket.id == ticket_id)
    if for_update and db.get_bind().dialect.name == "mysql":
        query = query.with_for_update()
    return query.first()


# ---------------------------------------------------------------------------
# 权限层：数据可见范围（普通用户仅限本人，管理员可全量）
# ---------------------------------------------------------------------------
#
# 角色怎么判由认证层定义，本层不再维护第二套口径：管理员与客户的边界只有一处
# 真相，避免工单侧认 admins、认证侧认 admin 这类拼写差异把隔离打穿。
# 本层只负责把角色换算成工单侧的语义——可见范围与单条数据的归属校验。


def is_admin(user: dict | None) -> bool:
    """判断当前登录用户是否为管理员（判定口径与认证层一致）。"""
    return auth_service.is_admin(user)


def user_scope(user: dict | None, include_admin_all: bool = True) -> str | None:
    """解析列表类查询的数据范围：None 表示全量，否则限定为该用户号。

    管理员在 include_admin_all 为真时看全量，为假时与普通用户一样只看本人；
    普通用户无论参数如何都只看本人。分页列表、待确认列表、统计共用这一口径，
    避免每个查询接口各写一遍角色判断，也不会出现某个接口漏掉隔离的情况。
    未登录直接拒绝，不返回范围不明的全量查询。
    """
    return auth_service.scope_user_id(user, include_admin_all=include_admin_all)


def ensure_ticket_access(
    user: dict | None,
    ticket: dict | None,
    action: str = "查看",
) -> None:
    """校验当前用户能否对该工单执行指定动作，越权时抛 TicketPermissionError。

    规则单一：管理员可操作任意工单，普通用户只能操作本人工单。校验失败时把
    动作、工单号与所属人放进错误详情，接口层既能提示用户，也能在日志里看清
    是谁访问了哪张单。工单为空时直接放行，由调用方区分「不存在」与「无权限」，
    保证 404 与 403 的语义不互相顶替。
    """
    if is_admin(user) or not ticket:
        return
    if ticket.get("user_id") != (user or {}).get("id"):
        raise TicketPermissionError(
            details={
                "action": action,
                "ticket_id": ticket.get("id"),
                "owner_id": ticket.get("user_id"),
            }
        )


# ---------------------------------------------------------------------------
# 认证联动层：登录身份 → 工单侧操作人
# ---------------------------------------------------------------------------
#
# 认证层回答「这张令牌是谁」，本层负责把答案换算成工单侧能直接用的操作人：
# 归属号、展示名与角色标记。写接口统一从这里取身份，不再各自到用户字典里
# 抠字段——少一处取值就少一处「字段缺失后被静默写成空串」的隐患，
# 也让建单、确认、取消这些动作的操作人落痕与登录身份始终对得上。

# 兜底操作人：没有真实操作人的场景（系统定时收敛、超时关闭）落到这个名称
DEFAULT_OPERATOR = "system"
# 操作人展示名长度上限：与用户名上限对齐，超长截断，避免写进时间线撑坏展示
OPERATOR_NAME_MAX_LENGTH = 32


def _clean_operator(value: str | None, fallback: str = DEFAULT_OPERATOR) -> str:
    """归一化操作人名称：压空白、截断超长、空值回落到兜底名称。"""
    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    return text[:OPERATOR_NAME_MAX_LENGTH]


def ticket_identity(user: dict | None) -> dict:
    """把登录用户换算成工单侧的操作人身份。

    返回 id（数据归属）、name（时间线展示名）、role（角色）与 is_admin；
    展示名优先取昵称，缺省回退用户名，再缺省回退归属号，保证时间线上不会
    出现一条没有操作人的空记录。身份缺失（未登录、令牌里没有主体）直接抛
    TicketAuthRequiredError：宁可让这次写入失败，也不拿一个空归属继续落数据。
    """
    account = user if isinstance(user, dict) else {}
    user_id = str(account.get("id") or "").strip()
    if not user_id:
        raise TicketAuthRequiredError(
            "登录状态缺失，请重新登录", details={"action": "身份校验"}
        )
    name = _clean_operator(
        account.get("name") or account.get("username"), fallback=user_id
    )
    role = auth_service.role_of(account)
    return {
        "id": user_id,
        "name": name,
        "role": role,
        "is_admin": role == auth_service.ADMIN_ROLE,
    }


# ---------------------------------------------------------------------------
# 远程接入层：把 Go 业务服务的写操作响应翻译成本层可处理的结果
# ---------------------------------------------------------------------------


class _RemoteWriteOutcome(NamedTuple):
    """Go 写操作的解析结果，ticket 与 not_found 互斥。

    「服务不可用」刻意不用本类型表达：那种情况由解析函数返回 None，
    调用方据此降级到本地实现。「没有结论」与「有结论但结果为空」是两件事，
    混在一起会让降级判断把「工单不存在」也当成故障重试一遍。
    """

    ticket: Optional[dict]
    not_found: bool


def _parse_remote_write(result: dict) -> Optional[_RemoteWriteOutcome]:
    """解析 Go 的写操作响应，三种出口。

    - 返回 outcome：拿到工单，或确认工单不存在；
    - 直接抛 TicketTransitionError：Go 判定「流转非法 / 状态未知」，
      异常类型与错误码和本地校验完全一致，接口层不需要为远程来源加分支；
    - 返回 None：响应无法识别（5xx、未知错误码、结构不完整），
      由调用方退回本地实现——远程只是加速，不能让它改变错误语义。
    """
    status = result.get("status")
    body = result.get("body")
    if not isinstance(body, dict):
        return None

    if status == 200 and isinstance(body.get("id"), str):
        return _RemoteWriteOutcome(body, False)

    code = body.get("error_code")
    detail = body.get("error_detail")
    detail = detail if isinstance(detail, dict) else {}

    if code == TicketErrorCode.NOT_FOUND or status == 404:
        return _RemoteWriteOutcome(None, True)

    if code in (TicketErrorCode.INVALID_TRANSITION, TicketErrorCode.UNKNOWN_STATUS):
        raise TicketTransitionError(
            str(detail.get("from_status", "")),
            str(detail.get("to_status", "")),
            message=body.get("error") or None,
            code=code,
        )

    return None


# ---------------------------------------------------------------------------
# 状态机层：工单状态变更的唯一写入口
# ---------------------------------------------------------------------------


def _log_status_change(
    db,
    ticket_id: str,
    from_status: str,
    to_status: str,
    operator: str = "system",
) -> None:
    """向时间线写入一条状态变更记录。

    所有写接口在状态发生变化的同一事务内调用本函数，保证状态与
    时间线落痕一致提交；原状态为「初始创建」时 from_status 记空串。
    """
    db.add(
        TicketStatusLog(
            ticket_id=ticket_id,
            from_status=from_status,
            to_status=to_status,
            operator=operator,
            changed_at=time.time(),
        )
    )


def _apply_transition(
    db,
    ticket,
    to_status: str,
    operator: str = "system",
    reply: dict | None = None,
) -> str:
    """状态机唯一入口：校验流转 → 追加说明 → 落时间线 → 改写状态。

    写入层的业务函数只描述「做什么」（确认、取消、回复、转人工），
    流转合法性、留痕与字段赋值统一收敛到这里，不再各写一遍。
    校验在改写任何字段之前完成，非法流转抛 TicketTransitionError 时
    工单保持原样；reply 非空时在同一事务内追加到回复列表。
    返回变更前的状态，调用方据此组织响应里的流转说明。
    """
    from_status = ticket.status
    validate_transition(from_status, to_status)
    if reply:
        replies = list(ticket.replies or [])
        replies.append(reply)
        ticket.replies = replies
    _log_status_change(db, ticket.id, from_status, to_status, operator=operator)
    ticket.status = to_status
    return from_status


# ---------------------------------------------------------------------------
# 写入层：建单与两阶段确认
# ---------------------------------------------------------------------------


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
    operator: str = DEFAULT_OPERATOR,
) -> tuple[dict, bool]:
    """创建工单（两阶段建单的第一阶段），返回 (工单数据, 是否新建)。

    建单先落地为待确认草稿（pending_confirm），不直接进入待处理（open）：
    草稿等待用户二次确认后，再经确认接口流转为 open 正式工单。

    同一用户同一问题（dedup_key）已有未解决工单时，直接返回原工单，
    并标记 created=False，调用方据此区分「新建」与「命中已有」，避免重复建单。

    operator 是首条状态时间线的操作人：带登录身份的调用方传真实建单人，
    定时任务等无身份场景沿用兜底名称。
    """
    description = (description or "").strip()
    if not description:
        raise TicketValidationError(
            "问题描述不能为空", code=TicketErrorCode.INVALID_DESCRIPTION
        )

    key = _dedup_key(user_id, description)
    try:
        # 建单临界区：SQLite 用库级写锁串行化；MySQL/PG 由 dedup_key 唯一约束兜底
        with advisory_lock(key) as db:
            mysql = db.get_bind().dialect.name == "mysql"
            query = db.query(Ticket).filter(Ticket.dedup_key == key)
            if mysql:
                lock_mysql_ticket_counter(db)
                query = query.with_for_update()
            existing = query.first()
            if existing:
                if existing.status != STATUS_RESOLVED:
                    return existing.to_dict(), False
                (
                    db.query(Ticket)
                    .filter(
                        Ticket.id == existing.id,
                        Ticket.dedup_key == key,
                        Ticket.status == STATUS_RESOLVED,
                    )
                    .update({Ticket.dedup_key: f"resolved:{existing.id}"}, synchronize_session=False)
                )

            ticket = Ticket(
                id=allocate_ticket_id(db),
                dedup_key=key,
                user_id=user_id,
                user_name=user_name,
                description=description,
                tried_steps=tried_steps,
                evidence=evidence,
                status=INITIAL_STATUS,
                created_at=time.time(),
                replies=[],
            )
            db.add(ticket)
            db.flush()
            db.refresh(ticket)
            # 建单即首次状态落位，写入首条时间线记录（from_status 为空表示初始创建）
            _log_status_change(
                db, ticket.id, "", INITIAL_STATUS, operator=_clean_operator(operator)
            )
            return ticket.to_dict(), True
    except IntegrityError:
        # 并发下另一请求已插入同一 dedup_key：唯一约束拦截，重查后返回已有工单
        db = SessionLocal()
        try:
            existing = (
                db.query(Ticket)
                .filter(Ticket.dedup_key == key, Ticket.status != STATUS_RESOLVED)
                .first()
            )
            if existing:
                return existing.to_dict(), False
            raise
        finally:
            db.close()


def create_ticket_for_user(
    user: dict | None,
    description: str,
    tried_steps: str = "",
    evidence: str = "",
) -> tuple[dict, bool, dict]:
    """以登录身份建单，归属与建单人一律取自登录态。

    入参可以是原始用户字典，也可以是 ticket_identity 已经换算好的身份块
    （两者字段同名，二次换算结果一致），接口层先换算再传即可避免重复取值。
    返回 (工单, 是否新建, 操作人身份)，身份块可直接回给前端用于确认归属。
    工单归属不接受外部指定：请求体里写谁的工单号都不影响结果，工单永远挂在
    当前登录账号名下，从入口上排除「替别人建单」这类越权写法。
    首条时间线记录真实建单人，客服复盘时能看清是谁提的单，
    而不是所有工单都显示由系统创建。
    """
    identity = ticket_identity(user)
    ticket, created = create_ticket(
        user_id=identity["id"],
        user_name=identity["name"],
        description=description,
        tried_steps=tried_steps,
        evidence=evidence,
        operator=identity["name"],
    )
    return ticket, created, identity


def confirm_ticket(ticket_id: str, operator: str = DEFAULT_OPERATOR) -> dict | None:
    """确认待确认草稿，将其正式流转为待处理工单（幂等）。

    两阶段建单的第二阶段：pending_confirm 草稿经用户二次确认后流转为 open。
    幂等语义：若工单已不在待确认状态（例如已 open、已关闭、已被并发确认），
    不抛错也不重复流转，直接原样返回当前工单，重复确认不会产生副作用。
    工单不存在时返回 None，由接口层区分「不存在」与「已确认」。
    operator 是确认动作的操作人：带登录身份的调用方传真实用户名，
    时间线据此区分「本人确认」与「系统确认」。

    配置了 GO_BUSINESS_URL 时优先调用 Go 业务服务；未配置、连不上、
    或响应无法识别时退回下面的本地实现。两条路径的状态机规则与返回结构
    完全一致，调用方不需要知道工单是哪边流转的。
    """
    cleaned = _clean_operator(operator)

    remote = go_client.confirm_ticket(ticket_id, cleaned)
    if remote is not None:
        outcome = _parse_remote_write(remote)
        if outcome is not None:
            return None if outcome.not_found else outcome.ticket

    with _ticket_session(write=True) as db:
        t = _load_ticket(db, ticket_id, for_update=True)
        if not t:
            return None
        # 已非待确认状态视为已确认/已终结，幂等返回，不重复流转
        if t.status != STATUS_PENDING_CONFIRM:
            return t.to_dict()
        _apply_transition(db, t, STATUS_OPEN, operator=cleaned)
        return t.to_dict()


def cancel_ticket(
    ticket_id: str,
    reason: str = "",
    operator: str = DEFAULT_OPERATOR,
) -> dict | None:
    """取消待确认草稿，将其流转为已关闭终态，并记录取消原因。

    两阶段建单的取消分支：pending_confirm 草稿由用户主动放弃后流转为 closed。
    取消原因以 system 来源写入 replies，时间线同时落一条流转记录，供后续复盘。
    幂等语义：若工单已不在待确认状态（已 open、已关闭、已被并发确认或取消），
    不抛错也不重复流转，原样返回当前工单；工单不存在时返回 None。
    取消说明的来源仍是系统记录，操作人落在时间线上，
    避免把一句系统说明算成一次人工回复。

    与确认一样优先走 Go 业务服务，失败退回本地：取消说明的文案、
    署名与时间戳口径两边保持一致。
    """
    cleaned = _clean_operator(operator)

    remote = go_client.cancel_ticket(ticket_id, reason, cleaned)
    if remote is not None:
        outcome = _parse_remote_write(remote)
        if outcome is not None:
            return None if outcome.not_found else outcome.ticket

    with _ticket_session(write=True) as db:
        t = _load_ticket(db, ticket_id, for_update=True)
        if not t:
            return None
        # 已非待确认状态视为已终结，幂等返回，不重复流转
        if t.status != STATUS_PENDING_CONFIRM:
            return t.to_dict()
        reply = {
            "replier": _SYSTEM_REPLIER,
            "content": f"取消工单：{reason}".strip(),
            "at": time.time(),
        }
        _apply_transition(
            db, t, STATUS_CLOSED, operator=cleaned, reply=reply
        )
        return t.to_dict()


def _expire_batch_size(limit) -> int:
    """校验单批收敛条数：非整数或越过 1–500 区间按入参错误拒绝。

    同类的批量归档接口（订单侧）也以 500 为上限，两处口径一致，
    调用方按同一个区间传参即可，不必记两套规则。
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise TicketValidationError(
            "单批收敛条数必须是整数",
            code=TicketErrorCode.INVALID_BATCH,
            details={"field": "limit", "value": limit},
        )
    if value < 1 or value > EXPIRE_BATCH_MAX:
        raise TicketValidationError(
            f"单批收敛条数需在 1–{EXPIRE_BATCH_MAX} 之间",
            code=TicketErrorCode.INVALID_BATCH,
            details={"field": "limit", "value": value},
        )
    return value


def _pending_timeout(timeout_seconds) -> int:
    """校验待确认超时阈值：未传取默认值，非整数或负数按入参错误拒绝。

    负数阈值没有业务含义，却会把「现在还没到期」的草稿全部判成超时——
    一次写错的参数就能立即批量关闭所有待确认工单，所以和 limit 一样
    当场拒绝，而不是悄悄拉回 0 继续执行。0 是合法值，表示「立即视为超时」。
    """
    if timeout_seconds is None:
        return PENDING_CONFIRM_TIMEOUT_SECONDS
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        raise TicketValidationError(
            "待确认超时阈值必须是整数",
            code=TicketErrorCode.INVALID_PAYLOAD,
            details={"field": "timeout_seconds", "value": timeout_seconds},
        )
    if timeout < 0:
        raise TicketValidationError(
            "待确认超时阈值不能为负数",
            code=TicketErrorCode.INVALID_PAYLOAD,
            details={"field": "timeout_seconds", "value": timeout},
        )
    return timeout


def expire_pending_tickets(
    timeout_seconds: int | None = None,
    limit: int | None = None,
    now: float | None = None,
) -> dict:
    """扫描超时未确认的待确认草稿，批量收敛为已关闭。

    超时判定以建单时间为基准：创建时间早于「当前时间 - 超时阈值」且仍处于
    pending_confirm 的工单视为超时草稿。收敛动作在单个事务内完成，每个工单
    写入一条取消说明与一条状态时间线，保证状态与落痕一致提交。

    并发控制：整个扫描收敛过程持有固定 key 的写锁，多个调度触发时串行执行，
    先到者收敛完毕，后到者按同条件重查已无候选，不会重复关闭。
    分批消化：单次最多处理 limit 条，返回的 remaining 表示仍有超时草稿待收敛，
    调用方可据此决定是否继续下一批。limit 越界（非整数、小于 1 或超过上限）
    按入参错误拒绝，不再静默拉回合法区间——批量接口一次改一批数据，
    参数写错应当立刻暴露，而不是让调用方以为传什么都生效。
    timeout_seconds 同理：负数或非整数直接拒绝，否则负阈值会把还没到期的
    草稿全部判成超时；0 保留为合法值，表示「立即视为超时」。

    返回扫描与收敛统计，closed_ids 为本次被关闭的工单号。
    """
    timeout = _pending_timeout(timeout_seconds)
    batch = EXPIRE_BATCH_LIMIT if limit is None else _expire_batch_size(limit)
    ts = time.time() if now is None else now
    deadline = ts - timeout

    with advisory_lock(_EXPIRE_LOCK_KEY) as db:
        query = (
            db.query(Ticket)
            .filter(Ticket.status == STATUS_PENDING_CONFIRM, Ticket.created_at <= deadline)
            .order_by(Ticket.created_at.asc())
            .limit(batch)
        )
        if db.get_bind().dialect.name == "mysql":
            query = query.with_for_update()
        candidates = query.all()
        closed_ids = []
        for t in candidates:
            # 锁内重查的状态为最终依据，已被确认或取消的工单不再处理
            if t.status != STATUS_PENDING_CONFIRM:
                continue
            reply = {
                "replier": _SYSTEM_REPLIER,
                "content": f"待确认超时（阈值 {timeout} 秒）自动关闭",
                "at": ts,
            }
            _apply_transition(db, t, STATUS_CLOSED, operator="system", reply=reply)
            closed_ids.append(t.id)

        # 会话关闭了自动 flush，先显式落库再统计，避免把本批已关闭的工单算进剩余
        db.flush()
        # 本次批量之外的超时草稿数量，供调用方判断是否需要继续下一批
        remaining = (
            db.query(func.count(Ticket.id))
            .filter(Ticket.status == STATUS_PENDING_CONFIRM, Ticket.created_at <= deadline)
            .scalar()
        )
        return {
            "timeout_seconds": timeout,
            "deadline": deadline,
            "scanned": len(candidates),
            "closed": len(closed_ids),
            "closed_ids": closed_ids,
            "remaining": int(remaining or 0),
        }


# ---------------------------------------------------------------------------
# 查询层：待确认草稿列表（只读，不改任何状态）
# ---------------------------------------------------------------------------


def _pending_item(t: Ticket, timeout: int, now: float) -> dict:
    """把待确认草稿组装为确认列表项。

    在工单原有字段外补充超时信息：expires_at 为该草稿的确认截止时间，
    remaining_seconds 为剩余可确认秒数（超时后归零），expired 标记是否已失效，
    前端据此展示倒计时或「已失效」提示。
    """
    data = t.to_dict()
    expires_at = (t.created_at or now) + timeout
    remaining = expires_at - now
    data["expires_at"] = expires_at
    data["remaining_seconds"] = max(0, int(remaining))
    data["expired"] = remaining <= 0
    return data


def list_pending_tickets(
    user_id: str | None = None,
    include_expired: bool = False,
    timeout_seconds: int | None = None,
    now: float | None = None,
) -> dict:
    """查询待确认草稿列表，供前端拉取确认列表。

    默认只返回仍可确认的草稿：超过待确认超时阈值的草稿已被超时收敛任务关闭，
    不再出现在列表里；include_expired=True 时一并返回已超时但尚未收敛的草稿，
    并以 expired / remaining_seconds 标记其状态，避免前端拿到无法确认的条目。
    user_id 非空时只返回该用户的草稿，用于普通用户视角的列表隔离；
    管理员需要全量视图时不传 user_id。列表按建单时间倒序，最新草稿排在最前。
    timeout_seconds 与超时收敛共用同一套校验：负数或非整数直接拒绝，
    避免查询视图和收敛视图对「是否超时」的判断出现分歧。

    优先调用 Go 业务服务的只读实现；未配置、调用失败或结构不符时退回本地查询。
    两种情形必须走本地：显式传入 now 的离线/测试场景（Go 用自己的时钟，无法对齐
    注入的时间口径），以及远程返回 None 的降级场景。参数校验始终在远程调用之前，
    非法入参不会被转发成另一个服务的错误。
    """
    timeout = _pending_timeout(timeout_seconds)
    ts = time.time() if now is None else now
    deadline = ts - timeout

    if now is None:
        remote = go_client.pending_tickets(
            user_id=user_id,
            include_expired=include_expired,
            timeout_seconds=timeout,
        )
        if remote is not None:
            return remote

    db = SessionLocal()
    try:
        query = db.query(Ticket).filter(Ticket.status == STATUS_PENDING_CONFIRM)
        if user_id:
            query = query.filter(Ticket.user_id == user_id)
        if not include_expired:
            # 建单时间晚于截止时间即为仍可确认的草稿
            query = query.filter(Ticket.created_at > deadline)
        rows = query.order_by(Ticket.created_at.desc()).all()
        items = [_pending_item(t, timeout, ts) for t in rows]
        return {
            "items": items,
            "total": len(items),
            "expired": sum(1 for item in items if item["expired"]),
            "timeout_seconds": timeout,
            "deadline": deadline,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 入参归一化层：状态、分页、排序、关键词等外部入参收敛为内部口径（纯函数）
# 每个归一化函数只做校验与转换，不访问数据库；非法入参统一抛 TicketValidationError
# ---------------------------------------------------------------------------


def normalize_status_filter(values: list[str] | str | None) -> list[str] | None:
    """把列表状态筛选入参归一化为去重后的状态列表。

    支持三种写法：单状态（status=open）、逗号分隔多状态（status=open,closed）、
    重复传参（status=open&status=closed）。每项先去空白并转小写，再去重，
    输出顺序与入参一致，便于响应回显本次生效的筛选条件。
    未传值或传 all 视为不筛选，返回 None；出现未知状态时抛 ValueError，
    由接口层转换成明确的错误信息，避免静默返回空列表。
    """
    if not values:
        return None
    if isinstance(values, str):
        values = [values]

    selected: list[str] = []
    for raw in values:
        for part in str(raw or "").split(","):
            status = part.strip().lower()
            if not status or status == "all":
                continue
            if not is_valid_status(status):
                raise TicketValidationError(
                    f"未知的工单状态：{status}", code=TicketErrorCode.INVALID_STATUS_FILTER
                )
            if status not in selected:
                selected.append(status)
    return selected or None


def normalize_pagination(page: int | None, page_size: int | None) -> tuple[int, int]:
    """校验并归一化分页参数，返回 (页码, 每页条数)。

    页码从 1 开始，小于 1 视为非法；每页条数默认 20，超过上限时拒绝而不是
    静默截断，避免调用方以为拿到了期望的条数。非法入参抛 ValueError，
    由接口层转成明确的错误信息。
    """
    current = 1 if page is None else int(page)
    if current < 1:
        raise TicketValidationError("页码必须大于等于 1", code=TicketErrorCode.INVALID_PAGINATION)
    size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
    if size < 1:
        raise TicketValidationError(
            "每页条数必须大于等于 1", code=TicketErrorCode.INVALID_PAGINATION
        )
    if size > MAX_PAGE_SIZE:
        raise TicketValidationError(
            f"每页条数不能超过 {MAX_PAGE_SIZE}", code=TicketErrorCode.INVALID_PAGINATION
        )
    return current, size


def normalize_order(order: str | None) -> str:
    """归一化排序方向：按建单时间正序（asc）或逆序（desc），默认逆序。"""
    if not order:
        return "desc"
    value = str(order).strip().lower()
    if value in ("asc", "ascending", "created_asc"):
        return "asc"
    if value in ("desc", "descending", "created_desc"):
        return "desc"
    raise TicketValidationError("排序方向只支持 asc 或 desc", code=TicketErrorCode.INVALID_ORDER)


def normalize_keyword(keyword: str | None) -> str | None:
    """归一化列表关键词：压缩连续空白、去首尾空白，空值表示不搜索。

    关键词按字面量参与匹配，其中的 % 与 _ 不作为通配符使用；
    超过长度上限的输入抛 ValueError，由接口层转成明确的错误信息，
    而不是静默截断后给出与预期不符的结果。
    """
    if keyword is None:
        return None
    value = " ".join(str(keyword).split())
    if not value:
        return None
    if len(value) > MAX_KEYWORD_LENGTH:
        raise TicketValidationError(
            f"关键词长度不能超过 {MAX_KEYWORD_LENGTH} 个字符",
            code=TicketErrorCode.INVALID_KEYWORD,
        )
    return value


# ---------------------------------------------------------------------------
# 查询层：条件拼装与分页列表（只读，不改任何状态）
# ---------------------------------------------------------------------------


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，让关键词中的 % _ \\ 按普通字符匹配。"""
    return (
        value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )


def _apply_keyword(query, keyword: str):
    """在查询上追加关键词过滤：工单号、问题描述、客户名任一命中即保留。

    匹配不区分大小写（SQLite 下由 ilike 转为 lower 比较），
    三个字段之间是「或」关系，与状态、用户、分页条件之间是「与」关系。
    """
    pattern = f"%{_escape_like(keyword)}%"
    return query.filter(
        or_(
            Ticket.id.ilike(pattern, escape=_LIKE_ESCAPE),
            Ticket.description.ilike(pattern, escape=_LIKE_ESCAPE),
            Ticket.user_name.ilike(pattern, escape=_LIKE_ESCAPE),
        )
    )


def list_tickets(
    statuses: list[str] | None = None,
    user_id: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    order: str | None = None,
    keyword: str | None = None,
) -> dict:
    """分页查询工单列表，支持按状态、所属用户、关键词筛选与时间正逆序排序。

    statuses 为空表示不按状态筛选；给出多个状态时按状态集合命中过滤，
    单状态与多状态走同一条查询路径。user_id 非空时只返回该用户的工单，
    把用户隔离下推到查询层，避免先全量取回再在内存里过滤。
    keyword 非空时按工单号、问题描述、客户名做模糊匹配，任一字段命中即保留，
    并与状态、用户、分页条件叠加生效。

    分页采用偏移量方式：total 为符合筛选条件的总条数（不受分页限制），
    pages 为总页数，has_more 标记后面是否还有下一页。排序以建单时间为准，
    并以工单号作为次序兜底，保证同一时间戳的记录在翻页时顺序稳定、不重不漏。
    """
    current, size = normalize_pagination(page, page_size)
    direction = normalize_order(order)
    search = normalize_keyword(keyword)

    db = SessionLocal()
    try:
        query = db.query(Ticket)
        if statuses:
            query = query.filter(Ticket.status.in_(statuses))
        if user_id:
            query = query.filter(Ticket.user_id == user_id)
        if search:
            query = _apply_keyword(query, search)

        total = query.count()
        order_column = Ticket.created_at.asc() if direction == "asc" else Ticket.created_at.desc()
        rows = (
            query.order_by(order_column, Ticket.id.asc())
            .offset((current - 1) * size)
            .limit(size)
            .all()
        )
        pages = (total + size - 1) // size
        return {
            "items": [t.to_dict() for t in rows],
            "total": total,
            "page": current,
            "page_size": size,
            "pages": pages,
            "has_more": current * size < total,
            "order": direction,
            "keyword": search,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 统计层：按状态计数与处理时长汇总
# ---------------------------------------------------------------------------


def normalize_stats_days(days: int | None) -> int | None:
    """校验统计区间：只统计最近 N 天创建的工单，未传表示不限制时间范围。

    只接受 1 到 MAX_STATS_DAYS 之间的整数；越界直接拒绝而不是静默截断，
    避免调用方以为拿到的是最近 N 天的统计、实际却是另一个区间。
    """
    if days is None:
        return None
    try:
        value = int(days)
    except (TypeError, ValueError):
        raise TicketValidationError(
            "统计天数必须是整数", code=TicketErrorCode.INVALID_STATS_RANGE
        )
    if value < 1 or value > MAX_STATS_DAYS:
        raise TicketValidationError(
            f"统计天数需在 1 到 {MAX_STATS_DAYS} 之间",
            code=TicketErrorCode.INVALID_STATS_RANGE,
            details={"min_days": 1, "max_days": MAX_STATS_DAYS, "requested_days": value},
        )
    return value


def format_duration(seconds: float | None) -> str:
    """把秒数格式化成便于阅读的时长，如「3 小时 20 分」「2 天 5 小时」。

    按天/时/分/秒逐级降档，只保留两个量级，避免出现「2 天 3 小时 15 分 8 秒」
    这类过细的读数；数值缺失时返回占位符，由调用方决定是否展示。
    """
    if seconds is None:
        return "—"
    total = int(round(max(float(seconds), 0)))
    if total < 60:
        return f"{total} 秒"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def _duration_summary(durations: list[float]) -> dict:
    """汇总一组处理时长样本：样本数、平均、最短、最长与中位数。

    空样本时各指标为 None，而不是填 0：0 会被误读成「平均处理时长 0 秒」，
    调用方据 sample_size 判断能否展示平均时长。
    """
    if not durations:
        return {
            "sample_size": 0,
            "total_seconds": None,
            "avg_seconds": None,
            "min_seconds": None,
            "max_seconds": None,
            "median_seconds": None,
            "avg_text": "—",
        }
    ordered = sorted(durations)
    total = sum(ordered)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) / 2
    average = total / len(ordered)
    return {
        "sample_size": len(ordered),
        "total_seconds": round(total, 3),
        "avg_seconds": round(average, 3),
        "min_seconds": round(ordered[0], 3),
        "max_seconds": round(ordered[-1], 3),
        "median_seconds": round(median, 3),
        "avg_text": format_duration(average),
    }


def _handling_durations(db, settled_tickets: list) -> dict[str, list[float]]:
    """按状态收集已结案工单的处理时长。

    处理时长 = 时间线中最后一次进入终态的时刻 - 建单时间。只统计已解决 / 已关闭
    的工单：仍在流转的工单没有结束时间，算进来会把平均值拉低。缺时间线记录或
    结束时间早于建单时间的脏数据直接跳过，不用建单时间自身凑出 0 秒样本。
    一次聚合查询取回全部结束时间，避免逐单回查数据库。
    """
    collected: dict[str, list[float]] = {status: [] for status in SETTLED_STATUSES}
    if not settled_tickets:
        return collected

    finished_at = dict(
        db.query(TicketStatusLog.ticket_id, func.max(TicketStatusLog.changed_at))
        .filter(
            TicketStatusLog.ticket_id.in_([t.id for t in settled_tickets]),
            TicketStatusLog.to_status.in_(SETTLED_STATUSES),
        )
        .group_by(TicketStatusLog.ticket_id)
        .all()
    )
    for ticket in settled_tickets:
        end = finished_at.get(ticket.id)
        if end is None or end < ticket.created_at:
            continue
        collected[ticket.status].append(end - ticket.created_at)
    return collected


def build_ticket_statistics(user_id: str | None = None, days: int | None = None) -> dict:
    """统计工单数量分布与平均处理时长。

    status_counts 固定包含全部状态（计数为 0 也出现），前端不必自己补零；
    status_breakdown 在计数之外给出占比与中文名，顺序与状态枚举一致。
    active / settled 把状态分成「仍需推进」与「已结束」两类，并给出闭环率，
    便于一眼看出积压量与结案进度。

    处理时长只统计已进入终态（已解决 / 已关闭）的工单，按状态分别汇总后再合并，
    响应里同时给出平均、最短、最长与中位数，样本数为 0 时各指标为 None。
    user_id 非空时只统计该用户的工单，days 非空时只统计最近 N 天创建的工单。
    """
    scope_days = normalize_stats_days(days)
    now = time.time()
    since = now - scope_days * 86400 if scope_days else None

    with _ticket_session() as db:
        query = db.query(Ticket)
        if user_id:
            query = query.filter(Ticket.user_id == user_id)
        if since is not None:
            query = query.filter(Ticket.created_at >= since)

        # 状态计数整组下推给数据库聚合：不再把整表（含回复 JSON 与长文本）拉回内存逐条数
        grouped = dict(
            query.with_entities(Ticket.status, func.count(Ticket.id))
            .group_by(Ticket.status)
            .all()
        )
        status_counts = {status.value: 0 for status in TicketStatus}
        for status_value, count in grouped.items():
            if status_value in status_counts:
                status_counts[status_value] = count

        total = sum(grouped.values())
        breakdown = [
            {
                "status": status.value,
                "label": STATUS_LABELS[status.value],
                "count": status_counts[status.value],
                "percent": round(status_counts[status.value] * 100 / total, 1) if total else 0.0,
            }
            for status in TicketStatus
        ]

        # 处理时长只需结算工单的三个轻量字段，不取回复与排查长文本
        settled_rows = (
            query.with_entities(Ticket.id, Ticket.status, Ticket.created_at)
            .filter(Ticket.status.in_(SETTLED_STATUSES))
            .all()
        )
        by_status = _handling_durations(db, settled_rows)

    all_durations = [d for values in by_status.values() for d in values]
    active_total = sum(status_counts[status] for status in ACTIVE_STATUSES)
    settled_total = sum(status_counts[status] for status in SETTLED_STATUSES)

    return {
        "total": total,
        "status_counts": status_counts,
        "status_breakdown": breakdown,
        "active": {
            "count": active_total,
            "statuses": list(ACTIVE_STATUSES),
            "percent": round(active_total * 100 / total, 1) if total else 0.0,
        },
        "settled": {
            "count": settled_total,
            "statuses": list(SETTLED_STATUSES),
            "percent": round(settled_total * 100 / total, 1) if total else 0.0,
            "completion_rate": round(settled_total * 100 / total, 1) if total else 0.0,
        },
        "handling_duration": {
            **_duration_summary(all_durations),
            "by_status": {status: _duration_summary(by_status[status]) for status in SETTLED_STATUSES},
        },
        "range": {"days": scope_days, "since": since, "until": now},
        "user_id": user_id,
        "generated_at": now,
    }


# ---------------------------------------------------------------------------
# 组装层：详情结构转换（纯函数，不访问数据库）
# ---------------------------------------------------------------------------


def _split_numbered(text: str) -> list[str]:
    """按正文里的连续编号把一个片段切成多条。

    仅当片段以编号开头时才按编号切分，避免把正文里的版本号、金额等
    「数字 + 标点」误当成列表序号；不以编号开头时整段视为一条。
    """
    matches = list(_NUMBER_PREFIX_RE.finditer(text))
    if not matches or matches[0].start() != 0:
        single = _ITEM_PREFIX_RE.sub("", text).strip()
        return [single] if single else []

    items: list[str] = []
    for position, matched in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        piece = text[matched.end() : end].strip()
        if piece:
            items.append(piece)
    return items


def _split_troubleshoot_items(raw: str | None) -> list[str]:
    """把一段排查文本切成条目片段。

    文本可能由多步合并写入（如「重启客户端 · 检查网络」），也可能是一段带
    序号的连续列表（如「1. 重启 2. 换网络」），因此先按通用分隔符切分，
    再对每个片段按编号二次切分，保证两种写法都能还原成独立条目。
    """
    if not raw:
        return []
    segments: list[str] = []
    for part in _ITEM_SEPARATOR_RE.split(str(raw)):
        text = part.strip()
        if text:
            segments.extend(_split_numbered(text))
    return segments


def _clean_troubleshoot_item(text: str) -> str:
    """清洗单条排查文本：剥离序号前缀、压缩空白、超长截断。"""
    value = _ITEM_PREFIX_RE.sub("", (text or "").strip())
    value = " ".join(value.split())
    if len(value) > MAX_TROUBLESHOOT_ITEM_LENGTH:
        # 留出一位给省略号，保证截断后长度仍不超过上限
        value = value[: MAX_TROUBLESHOOT_ITEM_LENGTH - 1].rstrip() + "…"
    return value


def parse_tried_steps(raw: str | None) -> list[dict]:
    """把已尝试步骤解析为结构化条目列表。

    每条包含自增序号与文本，重复条目只保留首次出现，
    条数上限为 MAX_TROUBLESHOOT_ITEMS，超出部分丢弃。
    """
    items: list[dict] = []
    seen: set[str] = set()
    for segment in _split_troubleshoot_items(raw):
        text = _clean_troubleshoot_item(segment)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append({"index": len(items) + 1, "text": text})
        if len(items) >= MAX_TROUBLESHOOT_ITEMS:
            break
    return items


def parse_evidence(raw: str | None) -> list[dict]:
    """把排查依据解析为结构化条目列表，并拆出可选的来源字段。

    依据里常见的写法是「结论（来源：产品手册第 3 节）」，解析时把括号内的
    来源剥离为独立字段，文本只保留结论本身；没有来源标注时 source 为空串。
    """
    items: list[dict] = []
    seen: set[str] = set()
    for segment in _split_troubleshoot_items(raw):
        text = segment
        source = ""
        matched = _SOURCE_RE.search(text)
        if matched:
            source = matched.group("source").strip()
            # 连同来源标注前的分隔空格一起去掉，避免留下悬空的标点
            text = (text[: matched.start()] + text[matched.end() :]).strip(" \t，,；;")
        text = _clean_troubleshoot_item(text)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append({"index": len(items) + 1, "text": text, "source": source})
        if len(items) >= MAX_TROUBLESHOOT_ITEMS:
            break
    return items


def build_troubleshooting(tried_steps: str | None, evidence: str | None) -> dict:
    """组装工单的排查信息结构块。

    原始文本字段仍按原样返回以兼容既有前端，这里额外给出结构化的步骤与依据
    条目及计数，详情页可直接逐条渲染，不必再自行切分字符串。
    """
    steps = parse_tried_steps(tried_steps)
    sources = parse_evidence(evidence)
    return {
        "tried_steps": steps,
        "evidence": sources,
        "tried_step_count": len(steps),
        "evidence_count": len(sources),
        "has_tried_steps": bool(steps),
        "has_evidence": bool(sources),
    }


def _status_logs(db, ticket_id: str):
    """查询工单的状态变更日志，按发生时间升序返回原始记录。

    排序下推到数据库，避免详情页把全量日志取回后在内存里再排一遍；
    这里只负责取数，转成响应结构由 build_status_timeline 完成。
    """
    return (
        db.query(TicketStatusLog)
        .filter(TicketStatusLog.ticket_id == ticket_id)
        .order_by(TicketStatusLog.changed_at.asc(), TicketStatusLog.id.asc())
        .all()
    )


def build_status_timeline(logs) -> list[dict]:
    """把状态日志转换为时间线条目，按变更时间升序、同刻按记录号升序。

    纯转换不访问数据库：入参是已查出的日志记录，便于单独验证顺序与字段。
    时间缺失的记录按最早处理并保持记录号次序，保证输出顺序稳定。
    """
    ordered = sorted(
        logs or [],
        key=lambda log: (getattr(log, "changed_at", None) or 0, getattr(log, "id", None) or 0),
    )
    return [log.to_dict() for log in ordered]


def _first_value(raw: dict, keys: tuple[str, ...], default=None):
    """按候选键顺序取出第一个非空值，用于兼容历史数据的字段别名。"""
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return default


def _format_reply_time(value) -> tuple[float | None, str]:
    """把回复时间归一化为 (时间戳, 本地时间字符串)。

    时间可能是秒级浮点时间戳，也可能是数字字符串；缺失或无法解析时
    时间戳返回 None、字符串返回空串，由调用方按「时间未知」处理。
    """
    if isinstance(value, bool):
        return None, ""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None, ""
    if ts <= 0:
        return None, ""
    return ts, datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _normalize_reply(raw, index: int) -> dict | None:
    """把一条原始回复归一化为时间线条目（序号/回复人/内容/时间/来源）。

    原始回复约定为 {replier, content, at}，同时兼容只存了纯文本的旧数据；
    内容为空的条目直接丢弃，避免详情里出现空白行。
    回复人非 system 时标记为人工回复，前端据此区分人工回复与系统说明。
    转人工说明带 kind 标记，其附带的转人工原因与命中规则码一并透出，
    详情页可直接展示判定依据，不必再回头解析说明文本。
    """
    extras: dict = {}
    if isinstance(raw, str):
        replier, content, at = "", raw, None
    elif isinstance(raw, dict):
        replier = _first_value(raw, _REPLIER_KEYS, "")
        content = _first_value(raw, _CONTENT_KEYS, "")
        at = _first_value(raw, _TIME_KEYS)
        kind = str(raw.get("kind") or "").strip()
        if kind:
            extras["kind"] = kind
            extras["reason"] = str(raw.get("reason") or "").strip()
            codes = raw.get("reason_codes") or []
            extras["reason_codes"] = [str(code) for code in codes]
            extras["operator"] = str(raw.get("operator") or "").strip()
    else:
        return None

    text = " ".join(str(content or "").split())
    if not text:
        return None
    name = str(replier or "").strip() or _SYSTEM_REPLIER
    is_human = name != _SYSTEM_REPLIER
    ts, ts_text = _format_reply_time(at)
    entry = {
        "index": index,
        "replier": name,
        "content": text,
        "at": ts,
        "at_text": ts_text,
        "source": "human" if is_human else "system",
        "is_human": is_human,
    }
    entry.update(extras)
    return entry


def build_reply_timeline(
    replies,
    created_at: float | None = None,
) -> dict:
    """组装工单的回复时间线，按回复发生顺序逐条返回。

    每条包含自增序号、回复人、内容、时间戳与本地时间字符串，并用 source /
    is_human 区分人工回复与系统说明（取消原因、超时关闭、转人工等）。
    同时给出人工/系统回复计数、首末回复时间与首次人工响应耗时，
    详情页与运营复盘都无需再自行遍历原始数组做统计。

    回复条数超过上限时只保留最近 MAX_REPLY_ITEMS 条并标记 truncated，
    序号按保留后的顺序重排，保证前端渲染的序号连续。
    """
    items: list[dict] = []
    for raw in list(replies or []):
        entry = _normalize_reply(raw, len(items) + 1)
        if entry:
            items.append(entry)

    truncated = len(items) > MAX_REPLY_ITEMS
    if truncated:
        items = items[-MAX_REPLY_ITEMS:]
        for position, entry in enumerate(items, 1):
            entry["index"] = position

    human_items = [entry for entry in items if entry["is_human"]]
    system_items = [entry for entry in items if not entry["is_human"]]
    # 转人工说明单列计数，便于详情页提示「本次处理已转人工」
    transfer_items = [entry for entry in items if entry.get("kind") == _TRANSFER_KIND]

    # 首次人工响应耗时：从建单到第一条人工回复的间隔，无人工回复或时间缺失时为空
    first_response = None
    if human_items and human_items[0]["at"] is not None and created_at:
        first_response = max(0, int(round(human_items[0]["at"] - created_at)))

    return {
        "items": items,
        "total": len(items),
        "human_count": len(human_items),
        "system_count": len(system_items),
        "has_replies": bool(items),
        "has_human_reply": bool(human_items),
        "first_reply_at": items[0]["at"] if items else None,
        "last_reply_at": items[-1]["at"] if items else None,
        "last_human_reply_at": human_items[-1]["at"] if human_items else None,
        "first_response_seconds": first_response,
        "transfer_count": len(transfer_items),
        "last_transfer_reason": transfer_items[-1].get("reason", "") if transfer_items else "",
        "truncated": truncated,
    }


def build_detail_summary(
    troubleshooting: dict,
    status_timeline: list[dict],
    reply_timeline: dict,
) -> dict:
    """汇总详情各分区的关键指标，供前端一屏概览。

    状态链路按流转顺序给出目标状态列表，last_changed_at 为最近一次变更时间，
    没有状态记录时置空；回复相关指标直接取自回复时间线的统计结果，
    避免调用方为了一个数字再遍历一遍原始数组。
    """
    return {
        "tried_step_count": troubleshooting["tried_step_count"],
        "evidence_count": troubleshooting["evidence_count"],
        "status_change_count": len(status_timeline),
        "status_chain": [item["to_status"] for item in status_timeline],
        "last_changed_at": status_timeline[-1]["changed_at"] if status_timeline else None,
        "reply_count": reply_timeline["total"],
        "human_reply_count": reply_timeline["human_count"],
        "system_reply_count": reply_timeline["system_count"],
        "has_human_reply": reply_timeline["has_human_reply"],
        "first_response_seconds": reply_timeline["first_response_seconds"],
        "transfer_count": reply_timeline["transfer_count"],
    }


def build_ticket_detail(ticket, status_logs=None) -> dict:
    """组装工单详情响应：基础字段 + 三个数据分区 + 指标汇总。

    三个分区各自独立组装、互不依赖，新增分区只需在此追加一次：
    - troubleshooting：已尝试步骤与排查依据的结构化条目
    - status_timeline：建单与每次流转的状态轨迹
    - reply_timeline：回复人、内容、时间与人工/系统分类
    基础字段沿用工单自身的序列化结果，原始文本字段照旧返回以兼容既有展示。
    """
    troubleshooting = build_troubleshooting(ticket.tried_steps, ticket.evidence)
    status_timeline = build_status_timeline(status_logs)
    reply_timeline = build_reply_timeline(ticket.replies, ticket.created_at)

    data = ticket.to_dict()
    data["troubleshooting"] = troubleshooting
    data["status_timeline"] = status_timeline
    data["reply_timeline"] = reply_timeline
    data["detail_summary"] = build_detail_summary(
        troubleshooting, status_timeline, reply_timeline
    )
    return data


# ---------------------------------------------------------------------------
# 查询层：详情取数（组装交给组装层，状态流转交给状态机层）
# ---------------------------------------------------------------------------


def get_ticket(ticket_id: str) -> dict | None:
    """查询工单详情：只负责取数，结构组装交给 build_ticket_detail。

    取数与组装分离后，组装逻辑可以脱离数据库单独验证；工单不存在时返回 None，
    由接口层区分「不存在」与「无权限」。
    """
    with _ticket_session() as db:
        t = _load_ticket(db, ticket_id)
        if not t:
            return None
        return build_ticket_detail(t, _status_logs(db, ticket_id))


# ---------------------------------------------------------------------------
# 写入层：回复、状态变更与转人工
# ---------------------------------------------------------------------------


def add_reply(ticket_id: str, replier: str, content: str) -> dict | None:
    """追加人工回复，并将待处理工单推进到处理中。

    状态变更统一走状态机入口：仅 open 状态允许伴随回复自动流转到 in_progress，
    其余非处理中状态由流转校验拒绝；已在处理中的工单只追加回复，不再产生流转。
    """
    with _ticket_session(write=True) as db:
        t = _load_ticket(db, ticket_id, for_update=True)
        if not t:
            return None
        reply = {"replier": replier, "content": content, "at": time.time()}
        if t.status == STATUS_IN_PROGRESS:
            replies = list(t.replies or [])
            replies.append(reply)
            t.replies = replies
        else:
            _apply_transition(db, t, STATUS_IN_PROGRESS, operator=replier, reply=reply)
        return t.to_dict()


def update_status(ticket_id: str, status: str) -> dict | None:
    """更新工单状态，变更统一交给状态机入口。

    未知状态值或非法流转会抛出 TicketTransitionError，由接口层捕获后
    返回明确错误信息，避免非法状态写入数据库。

    优先走 Go 业务服务：Go 侧「非法流转」返回 409、「未知状态」返回 422，
    与本地异常的错误码一一对应，因此远程判定失败也会抛出同一个异常类型，
    接口层的响应格式不会因为工单在哪边处理而改变。
    """
    remote = go_client.update_ticket_status(ticket_id, status)
    if remote is not None:
        outcome = _parse_remote_write(remote)
        if outcome is not None:
            return None if outcome.not_found else outcome.ticket

    with _ticket_session(write=True) as db:
        t = _load_ticket(db, ticket_id, for_update=True)
        if not t:
            return None
        _apply_transition(db, t, status, operator="system")
        return t.to_dict()


def transfer_to_human(
    ticket_id: str,
    reason: str = "",
    operator: str = "system",
    decision: dict | None = None,
) -> dict | None:
    """将工单转为人工处理，并把转人工原因写入回复时间线。

    转人工作为独立业务动作，需同时完成三件事：状态流转、原因留痕、责任人可追溯。
    原因为空时回填判定规则给出的原因文本，两者都没有时用默认说明兜底，
    保证每次转人工都有可读的原因，不会出现无说明的状态变更。
    原因以及命中的规则码一并写入 replies（结构化字段），时间线里可以还原
    「谁、因为什么、把工单从哪个状态转成了人工」。
    状态变更统一经过状态机入口，非法来源状态（已关闭、待确认等）由流转校验拒绝。
    """
    with _ticket_session(write=True) as db:
        t = _load_ticket(db, ticket_id, for_update=True)
        if not t:
            return None

        text = " ".join(str(reason or "").split())
        if not text and decision:
            text = " ".join(str(decision.get("reason_text") or "").split())
        if not text:
            text = DEFAULT_TRANSFER_REASON
        reason_codes = [str(code) for code in ((decision or {}).get("reason_codes") or [])]

        at = time.time()
        reply = {
            "replier": _SYSTEM_REPLIER,
            "content": f"转人工：{text}",
            "at": at,
            "kind": _TRANSFER_KIND,
            "reason": text,
            "reason_codes": reason_codes,
            "operator": operator,
        }
        from_status = _apply_transition(
            db, t, STATUS_TRANSFERRED_HUMAN, operator=operator, reply=reply
        )

        data = t.to_dict()
        data["transfer"] = {
            "from_status": from_status,
            "to_status": STATUS_TRANSFERRED_HUMAN,
            "reason": text,
            "reason_codes": reason_codes,
            "operator": operator,
            "at": at,
            "at_text": datetime.fromtimestamp(at).isoformat(timespec="seconds"),
            "by_rule": bool(decision and decision.get("should_transfer")),
        }
        return data


# ---------------------------------------------------------------------------
# 写入层：批量关闭与批量转人工（逐单独立结算，不因个别失败回滚整批）
# ---------------------------------------------------------------------------


def normalize_batch_ids(ticket_ids: list[str] | str | None) -> list[str]:
    """归一化批量操作的工单号列表：去空白、去重、限制单批数量。

    重复的工单号只保留首次出现，避免同一工单在同一批次里被处理两次；
    空列表与超过上限的批次直接拒绝，而不是静默截断，让调用方明确知道
    本次请求没有按预期执行。
    """
    if not ticket_ids:
        raise TicketValidationError(
            "工单号列表不能为空", code=TicketErrorCode.INVALID_BATCH
        )
    if isinstance(ticket_ids, str):
        ticket_ids = ticket_ids.split(",")

    ids: list[str] = []
    for raw in ticket_ids:
        ticket_id = str(raw or "").strip()
        if ticket_id and ticket_id not in ids:
            ids.append(ticket_id)

    if not ids:
        raise TicketValidationError(
            "工单号列表不能为空", code=TicketErrorCode.INVALID_BATCH
        )
    if len(ids) > MAX_BATCH_SIZE:
        raise TicketValidationError(
            f"单次批量操作不能超过 {MAX_BATCH_SIZE} 个工单，本次 {len(ids)} 个",
            code=TicketErrorCode.INVALID_BATCH,
            details={"max_batch_size": MAX_BATCH_SIZE, "requested": len(ids)},
        )
    return ids


def _batch_result(action: str, ids: list[str], success: list[str], failures: list[dict]) -> dict:
    """组装批量操作结果：请求数、成功数与失败明细。

    失败逐单返回工单号、错误码与原因，调用方可以只重试失败的部分，
    不必重新提交整批；action 标明本批执行的是关闭还是转人工。
    """
    return {
        "action": action,
        "requested": len(ids),
        "succeeded": len(success),
        "failed": len(failures),
        "success_ids": success,
        "failures": failures,
        "all_succeeded": not failures,
    }


def _batch_transition(
    ticket_ids: list[str] | str | None,
    to_status: str,
    action: str,
    reason: str,
    operator: str,
) -> dict:
    """批量把工单流转到目标状态，逐单结算成功与失败。

    整批共享一个写会话：成功的工单在同一事务内提交，个别工单的失败
    （不存在、当前状态不允许流转）只记入 failures，不影响同批其他工单。
    流转合法性仍由状态机层判定，这里捕获其业务异常转为失败明细，
    保证「非法流转」与「成功流转」在响应里都有明确归属。
    """
    ids = normalize_batch_ids(ticket_ids)
    text = " ".join(str(reason or "").split())

    success: list[str] = []
    failures: list[dict] = []

    with _ticket_session(write=True) as db:
        for ticket_id in ids:
            t = _load_ticket(db, ticket_id, for_update=True)
            if not t:
                failures.append(
                    {
                        "ticket_id": ticket_id,
                        "error_code": TicketErrorCode.NOT_FOUND,
                        "reason": "工单不存在",
                        "from_status": "",
                    }
                )
                continue

            from_status = t.status
            at = time.time()
            if to_status == STATUS_TRANSFERRED_HUMAN:
                content = f"转人工：{text}" if text else "转人工"
                reply = {
                    "replier": _SYSTEM_REPLIER,
                    "content": content,
                    "at": at,
                    "kind": _TRANSFER_KIND,
                    "reason": text or DEFAULT_TRANSFER_REASON,
                    "reason_codes": [],
                    "operator": operator,
                    "batch": True,
                }
            else:
                content = f"批量关闭：{text}" if text else "批量关闭工单"
                reply = {"replier": _SYSTEM_REPLIER, "content": content, "at": at, "batch": True}

            try:
                _apply_transition(db, t, to_status, operator=operator, reply=reply)
            except TicketTransitionError as exc:
                failures.append(
                    {
                        "ticket_id": ticket_id,
                        "error_code": exc.code,
                        "reason": exc.message,
                        "from_status": from_status,
                    }
                )
                continue
            success.append(ticket_id)

    return _batch_result(action, ids, success, failures)


def batch_close_tickets(
    ticket_ids: list[str] | str | None,
    reason: str = "",
    operator: str = "system",
) -> dict:
    """批量关闭工单，关闭原因写入每张工单的回复时间线。

    适合运营清理长期无响应的工单：一次提交多张工单，逐单落痕并记录操作人，
    已关闭或不可关闭的工单在 failures 里给出原因，不影响其他工单的关闭。
    """
    return _batch_transition(ticket_ids, STATUS_CLOSED, "close", reason, operator)


def batch_transfer_tickets(
    ticket_ids: list[str] | str | None,
    reason: str = "",
    operator: str = "system",
) -> dict:
    """批量转人工，转人工原因按转人工说明写入每张工单的时间线。

    原因为空时用默认说明兜底，保证每条转人工记录都可追溯；
    已终结或不允许转人工的工单进入 failures，其余工单照常流转。
    """
    return _batch_transition(
        ticket_ids, STATUS_TRANSFERRED_HUMAN, "transfer", reason, operator
    )


# ---------------------------------------------------------------------------
# 判定层：转人工规则（只读判定，是否真的转人工由写入层决定）
# ---------------------------------------------------------------------------


def detect_transfer_request(text: str | None) -> str:
    """识别文本中是否包含用户主动要求转人工的说法。

    命中时返回命中的说法，供判定结果回显依据；未命中返回空串。
    只做关键词匹配，不做语义推断，避免把普通问题描述误判为转人工诉求。
    """
    matched = _TRANSFER_REQUEST_RE.search(text or "")
    return matched.group(0) if matched else ""


def _check_service_scope(order: dict | None, feature: str) -> dict:
    """按订单信息判断当前问题是否超出服务范围。

    依次判断三点：有没有订单、订单是否在服务期内、问题涉及的功能是否在
    当前版本的能力范围内。feature 为空时跳过功能比对，只按订单与服务期判定。
    返回是否超范围、细分原因码与可直接展示的说明。
    单条订单视图里功能名总是能与能力列表比对出结果，因此 unverified 恒为
    False；「判不了」的结论只可能来自多订单判定。
    """
    if not order or not order.get("has_order"):
        return {
            "out_of_scope": True,
            "unverified": False,
            "code": "no_order",
            "detail": SCOPE_REASON_TEXT["no_order"],
        }
    if not order.get("in_service"):
        return {
            "out_of_scope": True,
            "unverified": False,
            "code": "service_expired",
            "detail": SCOPE_REASON_TEXT["service_expired"],
        }

    target = " ".join(str(feature or "").split())
    if target:
        features = [str(item) for item in (order.get("features") or [])]
        # 双向包含：用户说「导出」而版本特性写作「批量导出」时仍视为在范围内
        if not any(target in item or item in target for item in features):
            tier = order.get("product_name") or order.get("product_tier") or "当前版本"
            return {
                "out_of_scope": True,
                "unverified": False,
                "code": "feature_not_included",
                "detail": f"{tier}{SCOPE_REASON_TEXT['feature_not_included']}「{target}」",
            }
    return {"out_of_scope": False, "unverified": False, "code": "", "detail": ""}


def _check_order_scope(order_scope: dict, feature: str) -> dict:
    """按订单模块的多订单服务范围结论判断是否超出服务范围。

    与 _check_service_scope 的分工：那个函数只看单条订单视图，多产品用户会被
    主订单挡住；这里直接消费订单模块的综合判定（任一在期订单覆盖该功能即算在
    范围内），细分原因沿用订单侧结论原文，转人工说明与订单查询保持同一口径。
    没给功能时跳过功能比对，只按有无订单与是否存在在期订单判定。

    三种结论分开表达：out_of_scope 表示确有依据判定超范围；unverified 表示
    版本或功能名不可识别、暂时判不了，不能写成超出范围；都不是则在范围内。
    结论码为 scope_unverified，与订单侧原因码分开，读日志时不会把「识别不出
    功能名」误当成「客户没买这项服务」。
    """
    if not order_scope.get("has_order"):
        return {
            "out_of_scope": True,
            "unverified": False,
            "code": order_scope.get("reason_code") or "no_order",
            "detail": order_scope.get("reason") or SCOPE_REASON_TEXT["no_order"],
        }
    if not order_scope.get("in_service_count"):
        return {
            "out_of_scope": True,
            "unverified": False,
            "code": order_scope.get("reason_code") or "service_expired",
            "detail": order_scope.get("reason") or SCOPE_REASON_TEXT["service_expired"],
        }
    target = " ".join(str(feature or "").split())
    if target and not order_scope.get("in_scope"):
        order_code = str(order_scope.get("reason_code") or "")
        detail = order_scope.get("reason") or (
            f"{SCOPE_REASON_TEXT['feature_not_included']}「{target}」"
        )
        if order_code in UNVERIFIED_SCOPE_CODES:
            return {
                "out_of_scope": False,
                "unverified": True,
                "code": "scope_unverified",
                "detail": f"无法自动核实服务范围：{detail}",
            }
        return {
            "out_of_scope": True,
            "unverified": False,
            "code": order_code or "feature_not_included",
            "detail": detail,
        }
    return {"out_of_scope": False, "unverified": False, "code": "in_scope", "detail": ""}


def evaluate_transfer_rule(
    ticket: dict,
    order: dict | None = None,
    user_text: str = "",
    feature: str = "",
    tried_step_count: int | None = None,
    human_reply_count: int | None = None,
    order_scope: dict | None = None,
) -> dict:
    """按三条规则判定工单是否需要转人工，给出命中原因与判定依据。

    规则与优先级：
    1. 用户主动要求：用户消息或工单描述里出现转人工诉求，最优先，用户意愿
       不被其他规则覆盖；
    2. 超出服务范围：无订单、服务已到期、所问功能不在当前版本能力内；
       功能名或所购版本不可识别时只记为 scope_unverified（待人工核实），
       不写成超出范围，避免把「识别不出来」记成「没买这项服务」；
    3. 多次排查无效：已尝试步骤数达到阈值，或人工回复轮数达到阈值仍未闭环。

    判定只读不写：不改变工单状态，也不追加回复，是否真的转人工由调用方决定。
    工单已处终态或当前状态不允许流转到转人工时 applicable 为 False，
    直接给出「无需判定」，避免对已关闭、待确认的工单给出误导性建议。

    服务范围有两种来源：order_scope 是订单模块的多订单综合结论（多产品用户
    任一在期订单覆盖即算在范围内），优先使用；只给了单条订单视图 order 时，
    退回 _check_service_scope 按单订单判定。判定依据里附 service_scope 摘要，
    调用方能看到结论背后有几条订单、哪条订单覆盖了该功能。
    """
    status = ticket.get("status", "")
    applicable = can_transition(status, STATUS_TRANSFERRED_HUMAN)
    scope_verdict = (
        _check_order_scope(order_scope, feature)
        if order_scope is not None
        else _check_service_scope(order, feature)
    )

    tried = (
        len(parse_tried_steps(ticket.get("tried_steps")))
        if tried_step_count is None
        else max(0, int(tried_step_count))
    )
    human = (
        build_reply_timeline(ticket.get("replies"), ticket.get("created_at"))["human_count"]
        if human_reply_count is None
        else max(0, int(human_reply_count))
    )

    reasons: list[dict] = []
    keyword = ""
    if applicable:
        keyword = detect_transfer_request(
            f"{user_text}\n{ticket.get('description', '')}"
        )
        if keyword:
            reasons.append(
                {
                    "code": "user_requested",
                    "label": TRANSFER_REASON_LABELS["user_requested"],
                    "detail": f"命中「{keyword}」",
                }
            )

        if scope_verdict["out_of_scope"]:
            reasons.append(
                {
                    "code": "out_of_scope",
                    "label": TRANSFER_REASON_LABELS["out_of_scope"],
                    "detail": scope_verdict["detail"],
                }
            )
        elif scope_verdict.get("unverified"):
            # 判不了与服务范围不符是两件事：前者只是缺信息，说明里不写「超出范围」
            reasons.append(
                {
                    "code": "scope_unverified",
                    "label": TRANSFER_REASON_LABELS["scope_unverified"],
                    "detail": scope_verdict["detail"],
                }
            )

        # 排查无效的两种表现只记一条原因码，避免同一原因重复出现在原因文本里
        if tried >= TRANSFER_TRIED_STEP_THRESHOLD:
            reasons.append(
                {
                    "code": "ineffective_retries",
                    "label": TRANSFER_REASON_LABELS["ineffective_retries"],
                    "detail": f"已尝试 {tried} 步仍无结论",
                }
            )
        elif human >= TRANSFER_HUMAN_REPLY_THRESHOLD:
            reasons.append(
                {
                    "code": "ineffective_retries",
                    "label": TRANSFER_REASON_LABELS["ineffective_retries"],
                    "detail": f"已有 {human} 条人工回复仍未闭环",
                }
            )

    reason_text = "；".join(f"{item['label']}（{item['detail']}）" for item in reasons)
    if not applicable:
        message = f"工单当前状态为 {status}，无需做转人工判定"
    elif reasons:
        message = f"建议转人工：{reason_text}"
    else:
        message = "暂不需要转人工：问题在服务范围内，排查轮次也未达到阈值"

    if order_scope is not None:
        has_order = bool(order_scope.get("has_order"))
        in_service = bool(order_scope.get("in_service_count"))
    else:
        has_order = bool(order and order.get("has_order"))
        in_service = bool(order and order.get("has_order") and order.get("in_service"))

    basis = {
        "tried_step_count": tried,
        "tried_step_threshold": TRANSFER_TRIED_STEP_THRESHOLD,
        "human_reply_count": human,
        "human_reply_threshold": TRANSFER_HUMAN_REPLY_THRESHOLD,
        "feature": " ".join(str(feature or "").split()),
        "has_order": has_order,
        "in_service": in_service,
        "matched_keyword": keyword,
    }
    if order_scope is not None:
        # 附上订单侧的多订单摘要，让「为什么算超范围」能追到具体订单
        basis["service_scope"] = {
            "out_of_scope": scope_verdict["out_of_scope"],
            "unverified": bool(scope_verdict.get("unverified")),
            "code": scope_verdict["code"],
            "detail": scope_verdict["detail"],
            "order_count": order_scope.get("order_count", 0),
            "in_service_count": order_scope.get("in_service_count", 0),
            "covered_by": order_scope.get("covered_by"),
        }

    return {
        "ticket_id": ticket.get("id"),
        "status": status,
        "applicable": applicable,
        "should_transfer": bool(applicable and reasons),
        "suggested_status": STATUS_TRANSFERRED_HUMAN if (applicable and reasons) else "",
        "reason_codes": [item["code"] for item in reasons],
        "reasons": reasons,
        "reason_text": reason_text,
        "basis": basis,
        "message": message,
    }

