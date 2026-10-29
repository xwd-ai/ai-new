import hashlib
import re
import time
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import func, or_, text

from backend.database import SessionLocal
from backend.models import Ticket
from backend.models.ticket import TicketStatusLog, is_valid_status, validate_transition

# 工单持久化到 SQLite 数据库
# 一个工单对应一次「问题 → 排查 → 建单」的闭环
# 工单状态机：pending_confirm(待确认) -> open(待处理) -> in_progress(处理中) -> resolved(已解决)
# 建单前可挂起为 pending_confirm，等待用户确认后再进入 open
# 预置工单逻辑收敛到 backend.seed 模块

# 待确认草稿的超时阈值（秒）：超时未确认的草稿收敛为已关闭，避免僵尸工单堆积
PENDING_CONFIRM_TIMEOUT_SECONDS = 30 * 60
# 单次扫描最多收敛的工单数，分批消化积压，避免一次扫描长时间占用写锁
EXPIRE_BATCH_LIMIT = 200
# 超时收敛的写锁键：同一时刻只允许一个扫描任务在临界区内收敛
_EXPIRE_LOCK_KEY = "ticket:pending_confirm:expire"

# 列表分页默认值与单页上限：默认每页 20 条，上限 100 条，避免一次拉全表
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# 列表关键词长度上限：超长输入直接拒绝，避免构造超长匹配模式拖慢查询
MAX_KEYWORD_LENGTH = 50
# LIKE 转义符：关键词里的 % 与 _ 按字面量匹配，不当通配符使用
_LIKE_ESCAPE = "\\"

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


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


@contextmanager
def advisory_lock(key: str):
    """建单 advisory lock：串行化同一意图（dedup_key）的并发建单。

    SQLite 无 PG 的 advisory lock 原语，这里借数据库连接级的写锁实现等价效果：
    持锁期间独占建单临界区，同一 key 的重复请求排队串行执行，避免并发下重复建单。
    """
    db = SessionLocal()
    try:
        # BEGIN IMMEDIATE 立即获取写锁，其他会话的写操作在此阻塞直到本事务提交/回滚
        db.execute(text("BEGIN IMMEDIATE"))
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _next_id(db) -> str:
    """工单号自增：基于现有最大序号 +1。"""
    max_id = db.query(func.max(Ticket.id)).scalar()
    if not max_id:
        seq = 1000
    else:
        seq = int(max_id.replace("T-", ""))
    return f"T-{seq + 1}"


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


def create_ticket(
    user_id: str,
    user_name: str,
    description: str,
    tried_steps: str,
    evidence: str,
) -> tuple[dict, bool]:
    """创建工单（两阶段建单的第一阶段），返回 (工单数据, 是否新建)。

    建单先落地为待确认草稿（pending_confirm），不直接进入待处理（open）：
    草稿等待用户二次确认后，再经确认接口流转为 open 正式工单。

    同一用户同一问题（dedup_key）已有未解决工单时，直接返回原工单，
    并标记 created=False，调用方据此区分「新建」与「命中已有」，避免重复建单。
    """
    description = (description or "").strip()
    if not description:
        raise ValueError("问题描述不能为空")

    key = _dedup_key(user_id, description)
    # 两阶段建单：初始一律落待确认草稿，待确认接口流转为 open 后才成为正式工单
    initial_status = "pending_confirm"
    # 建单临界区接入 advisory lock：同一意图（dedup_key）的并发建单在此串行执行，
    # 先到者建单并提交，后到者重查即可命中已有工单，避免并发下重复建单。
    with advisory_lock(key) as db:
        existing = (
            db.query(Ticket)
            .filter(Ticket.dedup_key == key, Ticket.status != "resolved")
            .first()
        )
        if existing:
            return existing.to_dict(), False

        ticket = Ticket(
            id=_next_id(db),
            dedup_key=key,
            user_id=user_id,
            user_name=user_name,
            description=description,
            tried_steps=tried_steps,
            evidence=evidence,
            status=initial_status,
            created_at=time.time(),
            replies=[],
        )
        db.add(ticket)
        db.flush()
        db.refresh(ticket)
        # 建单即首次状态落位，写入首条时间线记录（from_status 为空表示初始创建）
        _log_status_change(db, ticket.id, "", initial_status, operator="system")
        return ticket.to_dict(), True


def confirm_ticket(ticket_id: str) -> dict | None:
    """确认待确认草稿，将其正式流转为待处理工单（幂等）。

    两阶段建单的第二阶段：pending_confirm 草稿经用户二次确认后流转为 open。
    幂等语义：若工单已不在待确认状态（例如已 open、已关闭、已被并发确认），
    不抛错也不重复流转，直接原样返回当前工单，重复确认不会产生副作用。
    工单不存在时返回 None，由接口层区分「不存在」与「已确认」。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        # 已非待确认状态视为已确认/已终结，幂等返回，不重复流转
        if t.status != "pending_confirm":
            return t.to_dict()
        validate_transition(t.status, "open")
        _log_status_change(db, t.id, t.status, "open", operator="system")
        t.status = "open"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def cancel_ticket(ticket_id: str, reason: str = "") -> dict | None:
    """取消待确认草稿，将其流转为已关闭终态，并记录取消原因。

    两阶段建单的取消分支：pending_confirm 草稿由用户主动放弃后流转为 closed。
    取消原因以 system 来源写入 replies，时间线同时落一条流转记录，供后续复盘。
    幂等语义：若工单已不在待确认状态（已 open、已关闭、已被并发确认或取消），
    不抛错也不重复流转，原样返回当前工单；工单不存在时返回 None。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        # 已非待确认状态视为已终结，幂等返回，不重复流转
        if t.status != "pending_confirm":
            return t.to_dict()
        validate_transition(t.status, "closed")
        replies = list(t.replies or [])
        entry = {"replier": "system", "content": f"取消工单：{reason}".strip(), "at": time.time()}
        replies.append(entry)
        t.replies = replies
        _log_status_change(db, t.id, t.status, "closed", operator="system")
        t.status = "closed"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


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
    调用方可据此决定是否继续下一批。

    返回扫描与收敛统计，closed_ids 为本次被关闭的工单号。
    """
    timeout = PENDING_CONFIRM_TIMEOUT_SECONDS if timeout_seconds is None else max(0, int(timeout_seconds))
    batch = EXPIRE_BATCH_LIMIT if limit is None else max(1, int(limit))
    ts = time.time() if now is None else now
    deadline = ts - timeout

    with advisory_lock(_EXPIRE_LOCK_KEY) as db:
        candidates = (
            db.query(Ticket)
            .filter(Ticket.status == "pending_confirm", Ticket.created_at <= deadline)
            .order_by(Ticket.created_at.asc())
            .limit(batch)
            .all()
        )
        closed_ids = []
        for t in candidates:
            # 锁内重查的状态为最终依据，已被确认或取消的工单不再处理
            if t.status != "pending_confirm":
                continue
            replies = list(t.replies or [])
            replies.append(
                {
                    "replier": "system",
                    "content": f"待确认超时（阈值 {timeout} 秒）自动关闭",
                    "at": ts,
                }
            )
            t.replies = replies
            _log_status_change(db, t.id, t.status, "closed", operator="system")
            t.status = "closed"
            closed_ids.append(t.id)

        # 会话关闭了自动 flush，先显式落库再统计，避免把本批已关闭的工单算进剩余
        db.flush()
        # 本次批量之外的超时草稿数量，供调用方判断是否需要继续下一批
        remaining = (
            db.query(func.count(Ticket.id))
            .filter(Ticket.status == "pending_confirm", Ticket.created_at <= deadline)
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
    """
    timeout = (
        PENDING_CONFIRM_TIMEOUT_SECONDS
        if timeout_seconds is None
        else max(0, int(timeout_seconds))
    )
    ts = time.time() if now is None else now
    deadline = ts - timeout

    db = SessionLocal()
    try:
        query = db.query(Ticket).filter(Ticket.status == "pending_confirm")
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
                raise ValueError(f"未知的工单状态：{status}")
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
        raise ValueError("页码必须大于等于 1")
    size = DEFAULT_PAGE_SIZE if page_size is None else int(page_size)
    if size < 1:
        raise ValueError("每页条数必须大于等于 1")
    if size > MAX_PAGE_SIZE:
        raise ValueError(f"每页条数不能超过 {MAX_PAGE_SIZE}")
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
    raise ValueError("排序方向只支持 asc 或 desc")


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
        raise ValueError(f"关键词长度不能超过 {MAX_KEYWORD_LENGTH} 个字符")
    return value


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
        rows = query.order_by(order_column, Ticket.id.asc()).offset((current - 1) * size).limit(size).all()
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
    """
    if isinstance(raw, str):
        replier, content, at = "", raw, None
    elif isinstance(raw, dict):
        replier = _first_value(raw, _REPLIER_KEYS, "")
        content = _first_value(raw, _CONTENT_KEYS, "")
        at = _first_value(raw, _TIME_KEYS)
    else:
        return None

    text = " ".join(str(content or "").split())
    if not text:
        return None
    name = str(replier or "").strip() or _SYSTEM_REPLIER
    is_human = name != _SYSTEM_REPLIER
    ts, ts_text = _format_reply_time(at)
    return {
        "index": index,
        "replier": name,
        "content": text,
        "at": ts,
        "at_text": ts_text,
        "source": "human" if is_human else "system",
        "is_human": is_human,
    }


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


def get_ticket(ticket_id: str) -> dict | None:
    """查询工单详情：只负责取数，结构组装交给 build_ticket_detail。

    取数与组装分离后，组装逻辑可以脱离数据库单独验证；工单不存在时返回 None，
    由接口层区分「不存在」与「无权限」。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        return build_ticket_detail(t, _status_logs(db, ticket_id))
    finally:
        db.close()


def add_reply(ticket_id: str, replier: str, content: str) -> dict | None:
    """追加人工回复，并将待处理工单推进到处理中。

    状态变更统一走流转校验：仅 open 状态允许伴随回复自动流转到 in_progress，
    其余状态回复时不改变状态（保持当前状态不变），非法流转由校验函数拒绝。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        replies = list(t.replies or [])
        replies.append({"replier": replier, "content": content, "at": time.time()})
        t.replies = replies
        if t.status != "in_progress":
            validate_transition(t.status, "in_progress")
            _log_status_change(db, t.id, t.status, "in_progress", operator=replier)
            t.status = "in_progress"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def update_status(ticket_id: str, status: str) -> dict | None:
    """更新工单状态，变更前统一经过合法流转校验。

    未知状态值或非法流转会抛出 TicketTransitionError，由接口层捕获后
    返回明确错误信息，避免非法状态写入数据库。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        validate_transition(t.status, status)
        _log_status_change(db, t.id, t.status, status, operator="system")
        t.status = status
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()


def transfer_to_human(ticket_id: str, reason: str = "") -> dict | None:
    """将工单转为人工处理，并把转人工原因写入回复时间线。

    转人工作为独立业务动作：入参原因会以 system 来源记录到 replies，
    随后工单状态流转到 transferred_human。状态变更统一经过流转校验，
    非法来源状态（如已关闭、已转人工的工单）由校验函数拒绝。
    """
    db = SessionLocal()
    try:
        t = db.query(Ticket).filter(Ticket.id == ticket_id).first()
        if not t:
            return None
        validate_transition(t.status, "transferred_human")
        replies = list(t.replies or [])
        entry = {"replier": "system", "content": f"转人工：{reason}".strip(), "at": time.time()}
        replies.append(entry)
        t.replies = replies
        _log_status_change(db, t.id, t.status, "transferred_human", operator="system")
        t.status = "transferred_human"
        db.commit()
        db.refresh(t)
        return t.to_dict()
    finally:
        db.close()
