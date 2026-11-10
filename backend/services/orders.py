"""订单与服务范围判定。

三层递进，后一层复用前一层的结论：

1. 版本能力矩阵：把「买的是哪个版本」翻译成一组能力编码。编码用「域.动作」
   的形式，改中文文案不影响判定，日志与落库也不会因为换个说法就对不上；
2. 服务期：把起止时间戳收敛成有效 / 未开始 / 已过期三种状态，附带总天数、
   剩余天数与已用比例，供话术与续费提示直接使用；
3. 综合判定：版本 + 服务期 + 具体功能，给出「能否按已购服务处理」的结论，
   以及一条可直接展示给用户的原因；
4. 过期与续费：把服务期换算成续费提醒状态，并支持把服务期已结束、状态仍是
   有效的订单归档为过期；
5. 功能服务范围判定：跨全部订单回答「某个功能能不能用」——任一在期订单的
   版本覆盖该功能即判为在范围内，与多订单能力并集的口径一致。

判定函数一律保持纯函数：只读入参，不查库、不写库；需要订单数据时由
query_order 取好后传入，便于单测、也便于对话链路按只读方式反复调用。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Optional

from backend.database import SessionLocal
from backend.models import Order, Product
from backend.models.order import (
    ORDER_STATUS_LABELS,
    OrderNotFoundError,
    OrderStatus,
    as_status,
    is_terminal,
    next_statuses,
    status_label,
    validate_transition,
)

# 产品与订单持久化到 SQLite 数据库
# 订单是「服务范围判定」的依据：确认用户购买的是不是专业版、是否在服务期内
# 预置产品与订单逻辑收敛到 backend.seed 模块


# ============================ 一、版本与能力映射 ============================

TIER_BASIC = "basic"
TIER_PRO = "pro"


@dataclass(frozen=True)
class Feature:
    """一项可销售的能力。

    code 是判定用的稳定标识，name 是给用户看的名称，aliases 收用户口语与
    历史叫法（「人工客服」「API」这类），让输入不必与文案逐字一致。
    """

    code: str
    name: str
    aliases: tuple[str, ...] = ()


FEATURE_BASIC_EXPORT = Feature("export.basic", "基础导出", ("导出", "单文件导出"))
FEATURE_SINGLE_UPLOAD = Feature("upload.single", "单文件上传", ("上传", "单文件上传"))
FEATURE_COMMUNITY_SUPPORT = Feature("support.community", "社区支持", ("社区支持", "社区"))
FEATURE_BATCH_EXPORT = Feature("export.batch", "批量导出")
FEATURE_ADVANCED_REPORT = Feature("report.advanced", "高级报表", ("报表",))
FEATURE_PRIORITY_SUPPORT = Feature("support.priority", "优先客服支持", ("优先客服", "人工客服"))
FEATURE_API_ACCESS = Feature("api.access", "API 接口", ("api", "接口"))

# 基础版：能用起来的最小集合
_BASIC_CAPABILITIES = (
    FEATURE_BASIC_EXPORT,
    FEATURE_SINGLE_UPLOAD,
    FEATURE_COMMUNITY_SUPPORT,
)

# 专业版在基础版之上叠加：批量导出、高级报表、优先客服、API 接口。
# 能力只增不减，避免升级版本反而丢掉已有功能。
_PRO_CAPABILITIES = _BASIC_CAPABILITIES + (
    FEATURE_BATCH_EXPORT,
    FEATURE_ADVANCED_REPORT,
    FEATURE_PRIORITY_SUPPORT,
    FEATURE_API_ACCESS,
)

TIER_FEATURES: dict[str, tuple[Feature, ...]] = {
    TIER_BASIC: _BASIC_CAPABILITIES,
    TIER_PRO: _PRO_CAPABILITIES,
}

TIER_LABELS = {TIER_BASIC: "基础版", TIER_PRO: "专业版"}

# 版本别名：接口、订单、用户话术里出现过的写法都收敛到两个标准取值
TIER_ALIASES = {
    "basic": TIER_BASIC,
    "standard": TIER_BASIC,
    "基础": TIER_BASIC,
    "基础版": TIER_BASIC,
    "pro": TIER_PRO,
    "professional": TIER_PRO,
    "premium": TIER_PRO,
    "专业": TIER_PRO,
    "专业版": TIER_PRO,
}

# 去重后按声明顺序排列，供能力解析遍历；顺序稳定，命中结果才可复现
_ALL_FEATURES: tuple[Feature, ...] = tuple(
    dict.fromkeys(
        feature for features in TIER_FEATURES.values() for feature in features
    )
)


def normalize_tier(tier) -> Optional[str]:
    """把版本入参收敛为 basic / pro，无法识别返回 None。

    大小写与首尾空白先归一；中文版本名按别名表映射，
    拼错的、留空的、历史脏数据一律判为未知，不猜也不落默认版本——
    猜错版本会让服务范围判定直接给出错误结论。
    """
    if not isinstance(tier, str):
        return None
    return TIER_ALIASES.get(tier.strip().lower())


def tier_label(tier) -> str:
    """版本显示名，未知版本返回「未知版本」。"""
    return TIER_LABELS.get(normalize_tier(tier), "未知版本")


def resolve_feature(text) -> Optional[Feature]:
    """把一段描述解析成能力项，解析不出返回 None。

    先按编码与全名精确匹配，避免标准名称被模糊规则带偏；
    再做包含匹配，并取命中最长的一条——「批量导出失败」应当落在批量导出上，
    而不是退化成基础导出。只做单向包含（能力名出现在描述里），
    否则一句「导出」会被更长的「批量导出」抢走，基础版用户反而被判为超范围。
    """
    value = " ".join(str(text or "").split()).lower()
    if not value:
        return None

    for feature in _ALL_FEATURES:
        if value in (feature.code.lower(), feature.name.lower()):
            return feature

    best: Optional[tuple[int, Feature]] = None
    for feature in _ALL_FEATURES:
        tokens = (feature.name.lower(),) + tuple(alias.lower() for alias in feature.aliases)
        for token in tokens:
            if token and token in value and (best is None or len(token) > best[0]):
                best = (len(token), feature)
    return best[1] if best else None


def tier_capabilities(tier) -> dict:
    """列出版本包含的能力，未知版本返回 known=False 与空列表。

    feature_codes 供判定与日志使用，features 是中文名，供话术直接拼接。
    """
    key = normalize_tier(tier)
    if key is None:
        return {"tier": None, "label": tier_label(tier), "known": False,
                "feature_codes": [], "features": []}
    features = TIER_FEATURES[key]
    return {
        "tier": key,
        "label": TIER_LABELS[key],
        "known": True,
        "feature_codes": [feature.code for feature in features],
        "features": [feature.name for feature in features],
    }


def has_capability(tier, feature) -> bool:
    """版本是否包含某项能力；功能名无法识别时按不包含处理。

    无法识别不等于包含：宁可让调用方去核实，也不放行一个判断不了的功能。
    """
    target = resolve_feature(feature)
    key = normalize_tier(tier)
    if target is None or key is None:
        return False
    return target in TIER_FEATURES[key]


# ============================ 二、服务期判定 ============================

SERVICE_ACTIVE = "active"
SERVICE_NOT_STARTED = "not_started"
SERVICE_EXPIRED = "expired"
SERVICE_UNKNOWN = "unknown"

SERVICE_STATUS_LABELS = {
    SERVICE_ACTIVE: "服务中",
    SERVICE_NOT_STARTED: "未开始",
    SERVICE_EXPIRED: "已到期",
    SERVICE_UNKNOWN: "无法判定",
}

DAY_SECONDS = 86400.0

# 订单状态只有 active 视为有效；退订、退款等由业务侧写入，一律不提供服务
ORDER_STATUS_ACTIVE = "active"


def _as_timestamp(value) -> Optional[float]:
    """把时间入参收敛成浮点时间戳，非数值或布尔值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _format_day(timestamp: float) -> str:
    """时间戳转本地日期，仅用于展示。"""
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def evaluate_service_period(started_at, expires_at, now: Optional[float] = None) -> dict:
    """判定服务期状态并给出天数口径。

    三种状态按时间先后划分：now < started_at 为未开始，
    started_at <= now < expires_at 为服务中，其余为已到期。
    边界取左闭右开：开始当天即为服务中，到期时刻当刻即为已到期。

    起止时间缺失或颠倒时返回 unknown 而不是硬算：这类订单说明数据本身有问题，
    按已到期处理会误伤用户，按服务中处理又会放开不该给的权益，交给人工核实更稳妥。
    """
    start = _as_timestamp(started_at)
    end = _as_timestamp(expires_at)
    current = time.time() if now is None else _as_timestamp(now)

    if start is None or end is None or current is None or end <= start:
        return {
            "status": SERVICE_UNKNOWN,
            "status_label": SERVICE_STATUS_LABELS[SERVICE_UNKNOWN],
            "started_at": start,
            "expires_at": end,
            "started_text": _format_day(start) if start is not None else "",
            "expires_text": _format_day(end) if end is not None else "",
            "days_total": 0,
            "days_used": 0,
            "days_remaining": 0,
            "percent_used": 0.0,
            "active": False,
            "expired": False,
            "not_started": False,
        }

    total_seconds = end - start
    if current < start:
        status = SERVICE_NOT_STARTED
    elif current >= end:
        status = SERVICE_EXPIRED
    else:
        status = SERVICE_ACTIVE

    # 已用天数向下取整、剩余天数向上取整：用户视角里不足一天也算还有一天
    if current <= start:
        days_used = 0
    else:
        days_used = int((min(current, end) - start) // DAY_SECONDS)
    days_remaining = 0 if current >= end else int(math.ceil((end - current) / DAY_SECONDS))

    covered = min(max(current, start), end)
    percent_used = round((covered - start) / total_seconds * 100, 1)

    return {
        "status": status,
        "status_label": SERVICE_STATUS_LABELS[status],
        "started_at": start,
        "expires_at": end,
        "started_text": _format_day(start),
        "expires_text": _format_day(end),
        "days_total": int(total_seconds // DAY_SECONDS),
        "days_used": days_used,
        "days_remaining": days_remaining,
        "percent_used": percent_used,
        "active": status == SERVICE_ACTIVE,
        "expired": status == SERVICE_EXPIRED,
        "not_started": status == SERVICE_NOT_STARTED,
    }


# ============================ 三、服务范围综合判定 ============================

SCOPE_REASONS = {
    "no_order": "未查询到订单记录，无法判断服务范围",
    "tier_unknown": "无法识别所购版本，需要人工核实购买情况",
    "order_inactive": "订单当前不是有效状态，服务已停止",
    "not_started": "服务期尚未开始",
    "expired": "服务期已结束",
    "period_unknown": "订单起止时间不完整，无法判断服务期",
    "feature_unknown": "无法识别要确认的功能",
    "feature_not_included": "当前版本不包含该功能",
    "in_service": "在服务范围内",
}


def evaluate_service_scope(
    order: Optional[dict],
    feature=None,
    now: Optional[float] = None,
) -> dict:
    """综合版本、服务期与具体功能，判断能否按已购服务处理。

    入参是订单记录（Order.to_dict 的形态），feature 可选：给了就额外判断该功能
    是否在当前版本能力内，不给只看整体服务状态。

    结论按业务优先级逐层收紧，命中即停：没有订单 → 版本不可识别 → 订单失效 →
    服务期未开始 / 已到期 / 无法判定 → 功能不可识别 / 不在能力内。
    先报最上游的问题，用户才知道该先解决哪一步；把「功能不在版本内」放在
    「服务已到期」之前，会让人误以为升级版本就能用。
    """
    scope = {
        "has_order": False,
        "order_id": None,
        "order_status": "",
        "tier": None,
        "tier_label": "未知版本",
        "known_tier": False,
        "period": None,
        "in_service": False,
        "feature_codes": [],
        "features": [],
        "feature": None,
        "feature_included": False,
        "allowed": False,
        "reason_code": "",
        "reason": "",
    }

    if not order or order.get("has_order") is False:
        scope["reason_code"] = "no_order"
        scope["reason"] = SCOPE_REASONS["no_order"]
        return scope

    capabilities = tier_capabilities(order.get("product_tier"))
    period = evaluate_service_period(order.get("started_at"), order.get("expires_at"), now)
    order_status = str(order.get("status") or "").strip().lower()
    status_blocked = bool(order_status) and order_status != ORDER_STATUS_ACTIVE

    scope.update(
        {
            "has_order": True,
            "order_id": order.get("id") or order.get("order_id"),
            "order_status": order_status,
            "tier": capabilities["tier"],
            "tier_label": capabilities["label"],
            "known_tier": capabilities["known"],
            "period": period,
            "feature_codes": capabilities["feature_codes"],
            "features": capabilities["features"],
        }
    )
    scope["in_service"] = period["active"] and not status_blocked

    # 功能入参统一压空白后保存原文：解析失败时也要把用户问的内容回显出来，
    # 否则「无法识别」这条结论看不出是针对哪句话给的
    query = " ".join(str(feature or "").split())
    target = resolve_feature(query) if query else None
    if query:
        included = (
            target is not None
            and capabilities["known"]
            and target.code in capabilities["feature_codes"]
        )
        scope["feature"] = {
            "query": query,
            "code": target.code if target else "",
            "name": target.name if target else "",
            "included": included,
        }
        scope["feature_included"] = included
    else:
        scope["feature_included"] = True

    # 放行需要同时满足三件事：版本可识别、服务在期内、功能在版本内。
    # 版本不可识别时必须判为不放行——判断不了版本还能不能用，等于判断不了权益，
    # 这里放开会让一个查不到产品配置的账号直接拿到全部能力。
    scope["allowed"] = (
        capabilities["known"] and scope["in_service"] and scope["feature_included"]
    )

    for code, hit in (
        ("tier_unknown", not capabilities["known"]),
        ("order_inactive", status_blocked),
        ("not_started", period["not_started"]),
        ("expired", period["expired"]),
        ("period_unknown", period["status"] == SERVICE_UNKNOWN),
        ("feature_unknown", bool(query) and target is None),
        ("feature_not_included", target is not None and not scope["feature_included"]),
    ):
        if hit:
            scope["reason_code"] = code
            scope["reason"] = _scope_reason_text(code, scope, target)
            return scope

    scope["reason_code"] = "in_service"
    scope["reason"] = SCOPE_REASONS["in_service"]
    return scope


def _scope_reason_text(code: str, scope: dict, target: Optional[Feature]) -> str:
    """把原因码补全成可展示的说明，带上版本名或功能名。"""
    text = SCOPE_REASONS[code]
    if code == "feature_unknown":
        return f"{text}「{scope['feature']['query']}」"
    if code == "feature_not_included":
        return f"{scope['tier_label']}{text}「{target.name}」"
    if code in ("not_started", "expired", "period_unknown"):
        period = scope["period"] or {}
        if code == "expired" and period.get("expires_text"):
            return f"{text}（{period['expires_text']}）"
        if code == "not_started" and period.get("started_text"):
            return f"{text}（{period['started_text']} 起）"
    return text


# ============================ 四、订单取数与挑选 ============================


def _get_product(db, tier: str) -> Optional[dict]:
    product = db.query(Product).filter(Product.id == tier).first()
    return product.to_dict() if product else None


def list_orders(user_id: str) -> list[dict]:
    """取用户的全部订单，按到期时间从晚到早排列，同到期时间按订单号升序。

    一个用户可以买多个产品，也可以续费多次，订单与用户因此是一对多关系。
    把确定的口径放在取数这一层，上层挑选与展示都不必再依赖数据库返回顺序。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(Order)
            .filter(Order.user_id == user_id)
            .order_by(Order.expires_at.desc(), Order.id.asc())
            .all()
        )
        return [row.to_dict() for row in rows]
    finally:
        db.close()


def _order_rank(order: dict, now: Optional[float] = None) -> tuple:
    """主订单排序键：服务中优先 → 订单状态有效 → 到期更晚 → 订单号更小。

    前三项仍不能区分时用订单号兜底，保证同一批数据每次挑出的是同一条；
    否则「当前在用哪个版本」这类结论会在两次查询之间来回跳。
    """
    scope = evaluate_service_scope(order, now=now)
    period = scope["period"] or {}
    expires = period.get("expires_at") or 0.0
    return (
        0 if scope["in_service"] else 1,
        1 if scope["reason_code"] == "order_inactive" else 0,
        -expires,
        str(order.get("id") or ""),
    )


def select_primary_order(orders, now: Optional[float] = None) -> Optional[dict]:
    """从多条订单中选一条作为主订单，空入参返回 None。

    主订单用于「当前用户在用什么产品」这类只能给单一答案的场合：
    优先取服务期内的，多个版本都在期时取到期最晚的，通常是最近一次续费或升级。
    """
    candidates = [order for order in orders or [] if order]
    if not candidates:
        return None
    return min(candidates, key=lambda order: _order_rank(order, now))


def get_order(user_id: str, order_id: Optional[str] = None) -> Optional[dict]:
    """取用户的一条订单：指定 order_id 时精确取，否则取主订单。

    指定了订单号但该订单不属于这个用户时返回 None，由调用方按「订单不存在」
    处理——别人的订单号不能因为格式正确就被放行。
    """
    orders = list_orders(user_id)
    if not orders:
        return None
    if order_id:
        return next((order for order in orders if order["id"] == order_id), None)
    return select_primary_order(orders)


def _order_scope(order: dict, now: Optional[float] = None) -> dict:
    """单条订单的服务范围摘要，供多订单列表逐条展示。"""
    scope = evaluate_service_scope(order, now=now)
    return {
        "order_id": order["id"],
        "product_tier": order["product_tier"],
        "tier": scope["tier"],
        "tier_label": scope["tier_label"],
        "order_status": scope["order_status"],
        "period": scope["period"],
        "in_service": scope["in_service"],
        "allowed": scope["allowed"],
        "reason_code": scope["reason_code"],
        "reason": scope["reason"],
    }


def list_order_scopes(user_id: str, now: Optional[float] = None) -> list[dict]:
    """列出用户全部订单及各自的服务范围，顺序与 list_orders 一致。"""
    return [_order_scope(order, now) for order in list_orders(user_id)]


def query_order(user_id: str) -> dict:
    """查询某用户的订单与服务范围，返回给 Agent 作为决策依据。

    返回字段保持既有契约不变，只把 in_service 的判断交给
    evaluate_service_scope：服务期的起止时间与订单状态一起看，
    服务期尚未开始的订单不再被当成在服务中。

    版本能力优先取产品表中的配置（运营可改），产品行缺失时退回内置能力矩阵，
    这样即使产品表为空，判定也不会退化成「没有任何功能」。
    """
    order = get_order(user_id)
    if not order:
        return {
            "has_order": False,
            "message": "未查询到该用户的订单记录",
        }

    db = SessionLocal()
    try:
        product = _get_product(db, normalize_tier(order["product_tier"]) or order["product_tier"])
    finally:
        db.close()

    scope = evaluate_service_scope(order)
    return {
        "has_order": True,
        "order_id": order["id"],
        "product_tier": order["product_tier"],
        "product_name": product["name"] if product else scope["tier_label"],
        "in_service": scope["in_service"],
        "features": product["features"] if product else scope["features"],
        "expires_at": order["expires_at"],
    }


def query_orders(user_id: str, now: Optional[float] = None) -> dict:
    """多订单视图：逐条摘要、主订单与在期版本的能力并集。

    用户同时持有多个在期订单时，可用功能取并集：基础版和专业版都在期，
    不该因为主订单挑中基础版就把批量导出判成不可用。
    能力列表沿用产品表配置（运营可改），产品行缺失时退回内置矩阵。
    """
    orders = list_orders(user_id)
    if not orders:
        return {
            "has_order": False,
            "order_count": 0,
            "in_service_count": 0,
            "primary_order_id": None,
            "orders": [],
            "features": [],
            "message": "未查询到该用户的订单记录",
        }

    items = [_order_scope(order, now) for order in orders]
    primary = select_primary_order(orders, now)
    active = [item for item in items if item["in_service"]]

    features: list[str] = []
    db = SessionLocal()
    try:
        for item in active:
            tier = item["tier"] or item["product_tier"]
            product = _get_product(db, tier)
            names = (
                product["features"]
                if product
                else tier_capabilities(item["product_tier"])["features"]
            )
            for name in names:
                if name not in features:
                    features.append(name)
    finally:
        db.close()

    if not active:
        message = f"共 {len(orders)} 个订单，当前均不在服务期内"
    elif len(active) == len(orders):
        message = f"共 {len(orders)} 个订单，均在服务期内"
    else:
        message = f"共 {len(orders)} 个订单，{len(active)} 个在服务期内"

    return {
        "has_order": True,
        "order_count": len(orders),
        "in_service_count": len(active),
        "primary_order_id": primary["id"] if primary else None,
        "orders": items,
        "features": features,
        "message": message,
    }


# ============================ 五、查询结论 ============================


def _scope_message(scope: dict) -> str:
    """把判定结论拼成一句可展示的中文说明。

    判定原因已经带了版本名与时间，正常情况下直接沿用；
    只有「在服务范围内」需要额外补上到期时间与剩余天数——
    这是用户最关心的信息，也是后续续费提示的落点。
    """
    if not scope["has_order"]:
        return scope["reason"]

    if scope["reason_code"] != "in_service":
        return scope["reason"]

    period = scope["period"] or {}
    target = scope["feature"]
    if target and target["included"]:
        head = f"{scope['tier_label']}包含「{target['name']}」"
    else:
        head = f"{scope['tier_label']}服务中"
    return (
        f"{head}，服务期至 {period['expires_text']}"
        f"（剩余 {period['days_remaining']} 天）"
    )


def describe_order(
    user_id: str,
    feature=None,
    now: Optional[float] = None,
    order_id: Optional[str] = None,
) -> dict:
    """查询用户订单，返回版本、服务期与功能范围的完整结论。

    与 query_order 的分工：query_order 面向页面展示，字段固定为既有契约；
    本函数面向「能不能用」的判断，把判定过程一并透出——
    版本、服务期、功能是否包含、原因码与一句可直接展示的说明。

    order_id 留空时判定主订单；指定后只判定那一条订单，
    多产品用户要核实某个具体订单时不必依赖主订单的挑选结果。

    先取订单再交给纯函数判定，取数与判定分开，判定逻辑可脱离数据库单测。
    """
    scope = evaluate_service_scope(get_order(user_id, order_id), feature, now)
    return {
        "has_order": scope["has_order"],
        "order_id": scope["order_id"],
        "tier": scope["tier"],
        "tier_label": scope["tier_label"],
        "order_status": scope["order_status"],
        "period": scope["period"],
        "features": list(scope["features"]),
        "feature": scope["feature"],
        "in_service": scope["in_service"],
        "allowed": scope["allowed"],
        "reason_code": scope["reason_code"],
        "reason": scope["reason"],
        "message": _scope_message(scope),
    }


# ============================ 六、订单状态流转 ============================

# 状态变更后的提示文案：既要说明结果，也要说清下一步能做什么
STATUS_CHANGE_MESSAGES = {
    OrderStatus.ACTIVE: "订单已恢复有效，服务按服务期继续提供",
    OrderStatus.EXPIRED: "订单已标记为过期，服务期结束后不再提供新服务，可提示用户续费",
    OrderStatus.CANCELLED: "订单已退订，服务立即停止，如需继续使用请重新购买",
}


def describe_status_flow(order: Optional[dict]) -> dict:
    """给出一条订单的状态流转说明：当前状态、是否终态、可流转到的状态。

    面向管理侧展示与二次确认：把状态机允许的动作交出去，
    调用方不必自己维护一份流转表，也就不会出现「界面给了按钮、接口却拒绝」。
    """
    if not order:
        return {
            "order_id": None,
            "status": "",
            "status_label": "未知状态",
            "known": False,
            "terminal": False,
            "allowed_statuses": [],
            "allowed_labels": [],
        }

    current = as_status(order.get("status"))
    allowed = next_statuses(current)
    return {
        "order_id": order.get("id"),
        "status": current.value if current else "",
        "status_label": status_label(current),
        "known": current is not None,
        "terminal": is_terminal(current),
        "allowed_statuses": allowed,
        "allowed_labels": [ORDER_STATUS_LABELS[OrderStatus(value)] for value in allowed],
    }


def _change_message(from_status, to_status, unchanged: bool) -> str:
    """拼状态变更结果说明，幂等命中时说清「没有改动」，避免被误读成变更失败。"""
    if unchanged:
        return f"订单已是{status_label(to_status)}状态，无需变更"
    head = f"订单状态已由{status_label(from_status)}变更为{status_label(to_status)}"
    return f"{head}。{STATUS_CHANGE_MESSAGES[OrderStatus(to_status)]}"


def change_order_status(
    order_id: str,
    target,
    *,
    operator: str = "system",
    user_id: Optional[str] = None,
    reason: str = "",
    now: Optional[float] = None,
) -> dict:
    """变更订单状态，写入前统一经过状态机校验。

    校验先行、写库在后：非法流转与未知状态直接抛出异常，数据库里的状态
    不会因为一次失败操作而被改坏，调用方也能拿到可分支的错误码。

    user_id 传入时按归属过滤：他人订单与不存在的订单返回同一个错误，
    避免用订单号试探别人的订单是否存在。

    目标状态与当前状态相同时按幂等处理，不写库也不报错——订单状态常由
    定时归档与人工操作并发触发，重复置为同一状态属于正常情况。

    reason 与 operator 只回显给调用方用于日志与审计，不落订单表：
    订单表记录的是当前状态，变更过程由业务侧的操作日志承载。
    """
    key = " ".join(str(order_id or "").split())
    if not key:
        raise OrderNotFoundError(order_id="", message="订单号不能为空")

    db = SessionLocal()
    try:
        query = db.query(Order).filter(Order.id == key)
        if user_id is not None:
            query = query.filter(Order.user_id == user_id)
        order = query.first()
        if order is None:
            raise OrderNotFoundError(order_id=key)

        from_status = order.status
        to_status = validate_transition(from_status, target)
        unchanged = to_status.value == as_status(from_status).value
        changed_at = time.time() if now is None else _as_timestamp(now)

        if not unchanged:
            order.status = to_status.value
            db.commit()
        else:
            db.rollback()

        return {
            "order_id": order.id,
            "user_id": order.user_id,
            "from_status": from_status,
            "to_status": to_status.value,
            "status_label": ORDER_STATUS_LABELS[to_status],
            "unchanged": unchanged,
            "operator": " ".join(str(operator or "").split()) or "system",
            "reason": " ".join(str(reason or "").split()),
            "changed_at": changed_at,
            "allowed_statuses": next_statuses(to_status),
            "message": _change_message(from_status, to_status, unchanged),
        }
    finally:
        db.close()


# ============================ 七、过期归档与续费提示 ============================

# 续费提醒状态。active 与 none 都不提醒，区别在于「有订单且还早」与「压根没有订单」；
# due 与 upcoming 提醒，前者需要立即续费，后者是提前告知。
RENEWAL_NONE = "none"
RENEWAL_ACTIVE = "active"
RENEWAL_UPCOMING = "upcoming"
RENEWAL_DUE = "due"
RENEWAL_NOT_STARTED = "not_started"
RENEWAL_BLOCKED = "blocked"
RENEWAL_UNKNOWN = "unknown"

RENEWAL_LABELS = {
    RENEWAL_NONE: "无订单",
    RENEWAL_ACTIVE: "服务中",
    RENEWAL_UPCOMING: "即将到期",
    RENEWAL_DUE: "已到期",
    RENEWAL_NOT_STARTED: "未开始",
    RENEWAL_BLOCKED: "已退订",
    RENEWAL_UNKNOWN: "无法判定",
}

# 默认提前 30 天提示续费：留出一次完整的续费沟通与付款周期
RENEWAL_WINDOW_DAYS = 30

# 剩余天数落在这个区间内按高优先级提示，提醒文案里会给出具体天数
RENEWAL_URGENT_DAYS = 7

# 提醒级别，供前端决定用什么颜色与是否弹窗
URGENCY_NONE = "none"
URGENCY_INFO = "info"
URGENCY_WARNING = "warning"
URGENCY_CRITICAL = "critical"

# 建议动作：调用方按它决定引导用户去续费、等待生效还是重新下单
ACTION_RENEW = "renew"
ACTION_WAIT = "wait"
ACTION_BUY_NEW = "buy_new"
ACTION_VERIFY = "verify"
ACTION_NONE = "none"


def _normalize_window(days) -> int:
    """把提醒窗口收敛成 0–365 的整数天数，非法入参回落到默认窗口。

    窗口是展示口径而非业务规则，传错不值得报错中断一次查询；
    但也不能无限放大，否则「提前三年提醒续费」等同于每次都在提醒。
    """
    if isinstance(days, bool) or not isinstance(days, int):
        return RENEWAL_WINDOW_DAYS
    return max(0, min(days, 365))


def evaluate_renewal(
    order: Optional[dict],
    now: Optional[float] = None,
    window_days: Optional[int] = None,
) -> dict:
    """判断一条订单的续费提醒状态。

    入参是订单记录（Order.to_dict 的形态），订单为 None 时按「无订单」处理。
    判定顺序与业务处理顺序一致：退订订单不再提示续费 → 订单状态已归档为过期
    → 服务期无法判定 → 未开始 → 已到期 → 即将到期 → 还早。

    退订放在最前面判断：用户已经明确不要了，继续提醒续费只会造成骚扰，
    这类订单要引导的是重新购买而不是续费。
    """
    window = _normalize_window(window_days)
    hint = {
        "order_id": order.get("id") if order else None,
        "product_tier": order.get("product_tier") if order else "",
        "tier_label": "未知版本",
        "order_status": "",
        "state": RENEWAL_NONE,
        "state_label": RENEWAL_LABELS[RENEWAL_NONE],
        "remind": False,
        "urgency": URGENCY_NONE,
        "suggested_action": ACTION_NONE,
        "window_days": window,
        "days_remaining": 0,
        "days_overdue": 0,
        "expires_text": "",
        "message": "未查询到订单记录",
    }
    if not order:
        return hint

    scope = evaluate_service_scope(order, now=now)
    period = scope["period"] or {}
    status = as_status(order.get("status"))
    hint.update(
        {
            "tier_label": scope["tier_label"],
            "order_status": scope["order_status"],
            "expires_text": period.get("expires_text", ""),
            "days_remaining": period.get("days_remaining", 0),
        }
    )

    def settle(state: str, urgency: str, action: str, message: str) -> dict:
        hint.update(
            {
                "state": state,
                "state_label": RENEWAL_LABELS[state],
                "remind": state in (RENEWAL_UPCOMING, RENEWAL_DUE),
                "urgency": urgency,
                "suggested_action": action,
                "message": message,
            }
        )
        return hint

    if status == OrderStatus.CANCELLED:
        return settle(
            RENEWAL_BLOCKED,
            URGENCY_NONE,
            ACTION_BUY_NEW,
            "订单已退订，不再提供服务，如需继续使用请重新购买",
        )

    if status == OrderStatus.EXPIRED:
        return settle(
            RENEWAL_DUE,
            URGENCY_CRITICAL,
            ACTION_RENEW,
            f"订单已标记为过期，续费后恢复服务（原到期时间 {period['expires_text']}）"
            if period.get("expires_text")
            else "订单已标记为过期，续费后恢复服务",
        )

    if period.get("status") == SERVICE_UNKNOWN:
        return settle(
            RENEWAL_UNKNOWN,
            URGENCY_NONE,
            ACTION_VERIFY,
            "订单起止时间不完整，无法判断续费时间，需要人工核实",
        )

    if period["not_started"]:
        return settle(
            RENEWAL_NOT_STARTED,
            URGENCY_NONE,
            ACTION_WAIT,
            f"服务将于 {period['started_text']} 开始，暂无需续费",
        )

    if period["expired"]:
        current = time.time() if now is None else _as_timestamp(now)
        current = time.time() if current is None else current
        hint["days_overdue"] = max(0, int((current - period["expires_at"]) // DAY_SECONDS))
        return settle(
            RENEWAL_DUE,
            URGENCY_CRITICAL,
            ACTION_RENEW,
            f"服务已于 {period['expires_text']} 到期"
            f"（已过期 {hint['days_overdue']} 天），续费后可继续使用",
        )

    remaining = period["days_remaining"]
    if remaining <= RENEWAL_URGENT_DAYS:
        return settle(
            RENEWAL_UPCOMING,
            URGENCY_CRITICAL,
            ACTION_RENEW,
            f"服务将于 {period['expires_text']} 到期，仅剩 {remaining} 天，请尽快续费",
        )
    if remaining <= window:
        return settle(
            RENEWAL_UPCOMING,
            URGENCY_WARNING,
            ACTION_RENEW,
            f"服务将于 {period['expires_text']} 到期（剩余 {remaining} 天），建议提前续费",
        )
    return settle(
        RENEWAL_ACTIVE,
        URGENCY_NONE,
        ACTION_NONE,
        f"服务中，服务期至 {period['expires_text']}（剩余 {remaining} 天）",
    )


def list_renewal_hints(
    user_id: str,
    now: Optional[float] = None,
    window_days: Optional[int] = None,
) -> dict:
    """列出用户全部订单的续费提醒，并给出需要处理的订单数量。

    顺序沿用 list_orders（到期时间从晚到早），同一批数据每次返回一致；
    due 与 upcoming 分开计数，前端可以只弹「已到期」这一类，
    把「即将到期」留在列表里提示，避免一进页面就被弹窗拦住。
    """
    rows = list_orders(user_id)
    hints = [evaluate_renewal(order, now, window_days) for order in rows]
    due = [hint for hint in hints if hint["state"] == RENEWAL_DUE]
    upcoming = [hint for hint in hints if hint["state"] == RENEWAL_UPCOMING]
    window = _normalize_window(window_days)

    if not rows:
        message = "未查询到订单记录，暂无续费提示"
    elif due and upcoming:
        message = f"{len(due)} 个订单已到期，另有 {len(upcoming)} 个将在 {window} 天内到期"
    elif due:
        message = f"{len(due)} 个订单已到期，请及时续费"
    elif upcoming:
        message = f"{len(upcoming)} 个订单将在 {window} 天内到期，建议提前续费"
    else:
        message = f"共 {len(rows)} 个订单，暂无需续费"

    return {
        "has_order": bool(rows),
        "order_count": len(rows),
        "remind_count": len(due) + len(upcoming),
        "due_count": len(due),
        "upcoming_count": len(upcoming),
        "window_days": window,
        "hints": hints,
        "message": message,
    }


def expire_overdue_orders(
    user_id: Optional[str] = None,
    now: Optional[float] = None,
    limit: int = 100,
) -> dict:
    """把服务期已结束、状态仍是有效的订单归档为过期。

    先按条件查出候选订单号再逐条流转，不在同一次查询里批量 UPDATE：
    每条都要过状态机校验，归档动作与人工改状态走同一条路径，
    不会出现「定时任务写进去的状态状态机不认」这种分叉。

    单次最多处理 limit 条，把执行时间控制在一次请求内；
    剩下的订单留给下一次调用，调用方按 remaining 决定是否继续。

    归档只改订单状态，不动服务期时间：到期时间属于合同事实，
    续费是写入新订单或延长服务期，两者不能混在一次操作里。
    """
    size = max(1, min(int(limit or 100), 500))
    current = time.time() if now is None else _as_timestamp(now)
    if current is None:
        current = time.time()

    db = SessionLocal()
    try:
        query = db.query(Order).filter(
            Order.status == ORDER_STATUS_ACTIVE,
            Order.expires_at <= current,
        )
        if user_id is not None:
            query = query.filter(Order.user_id == user_id)
        candidates = [
            row.id
            for row in query.order_by(Order.expires_at.asc(), Order.id.asc())
            .limit(size + 1)
            .all()
        ]
    finally:
        db.close()

    batch = candidates[:size]
    archived: list[str] = []
    skipped: list[str] = []
    for order_id in batch:
        try:
            result = change_order_status(
                order_id,
                OrderStatus.EXPIRED,
                operator="system",
                user_id=user_id,
                reason="服务期结束自动归档",
                now=current,
            )
        except OrderStatusTransitionError:
            skipped.append(order_id)
            continue
        if result["unchanged"]:
            skipped.append(order_id)
        else:
            archived.append(order_id)

    remaining = max(0, len(candidates) - len(batch))
    if archived:
        message = f"已归档 {len(archived)} 个到期订单"
        if skipped:
            message += f"，{len(skipped)} 个无需变更"
    else:
        message = "没有需要归档的到期订单"

    return {
        "limit": size,
        "scanned": len(batch),
        "remaining": remaining,
        "has_more": remaining > 0,
        "archived": archived,
        "skipped": skipped,
        "message": message,
    }


# ============================ 八、功能服务范围判定 ============================

FEATURE_SCOPE_REASONS = {
    "feature_required": "请提供要确认的功能名称",
}


def _feature_scope_message(target: Feature, scope: dict) -> str:
    """拼「功能在服务范围内」的展示文案，带上覆盖它的版本与服务期。"""
    period = scope.get("period") or {}
    if period.get("expires_text"):
        return (
            f"「{target.name}」在服务范围内（{scope['tier_label']}，"
            f"服务期至 {period['expires_text']}，剩余 {period['days_remaining']} 天）"
        )
    return f"「{target.name}」在服务范围内"


def evaluate_feature_scope(
    orders: Optional[list] = None,
    feature=None,
    now: Optional[float] = None,
) -> dict:
    """跨全部订单判定某功能是否在服务范围内。

    与 evaluate_service_scope 的分工：那个函数只看一条订单，多产品用户里
    「主订单是基础版、另有一条在期专业版」时会把批量导出误判为不可用。
    本函数把用户全部订单放在一起看：任一在期订单的版本包含该功能即判为
    在范围内，covered_by 给出具体是哪条订单覆盖的，口径与多订单能力并集一致。

    不在范围内时按「先报最上游问题」只给一个原因码：
    没有订单 → 未给功能 → 功能不可识别 → 有在期订单但都不包含（取在期主订单
    的原因，版本不可识别也落在这里）→ 没有在期订单（取主订单的服务态原因）。
    逐单明细照常返回，个别订单的到期或退订情况由调用方按明细展示。
    """
    query = " ".join(str(feature or "").split())
    rows = [
        (order, evaluate_service_scope(order, query or None, now))
        for order in (orders or [])
        if order
    ]
    items = [
        {
            "order_id": scope["order_id"],
            "tier_label": scope["tier_label"],
            "order_status": scope["order_status"],
            "in_service": scope["in_service"],
            "feature_included": scope["feature_included"],
            "reason_code": scope["reason_code"],
            "reason": scope["reason"],
        }
        for _, scope in rows
    ]
    active = [(order, scope) for order, scope in rows if scope["in_service"]]
    covered = [(order, scope) for order, scope in active if scope["feature_included"]]

    result = {
        "has_order": bool(rows),
        "feature": {
            "query": query,
            "code": "",
            "name": "",
            # included 是「版本层是否含该功能」，与单订单判定同口径；
            # 未给功能时没有可判定的对象，保持 false
            "included": bool(query) and any(item["feature_included"] for item in items),
        },
        "in_scope": False,
        "covered_by": None,
        "order_count": len(rows),
        "in_service_count": len(active),
        "reason_code": "",
        "reason": "",
        "message": "",
        "orders": items,
    }

    def settle(code: str, reason: str, covered_by: Optional[str] = None) -> dict:
        result["reason_code"] = code
        result["reason"] = reason
        result["message"] = reason
        if code == "in_scope":
            result["in_scope"] = True
            result["covered_by"] = covered_by
        return result

    if not rows:
        return settle("no_order", SCOPE_REASONS["no_order"])
    if not query:
        return settle("feature_required", FEATURE_SCOPE_REASONS["feature_required"])

    target = resolve_feature(query)
    if target is None:
        return settle(
            "feature_unknown", f"{SCOPE_REASONS['feature_unknown']}「{query}」"
        )
    result["feature"]["code"] = target.code
    result["feature"]["name"] = target.name

    if covered:
        order, scope = min(covered, key=lambda pair: _order_rank(pair[0], now))
        return settle("in_scope", _feature_scope_message(target, scope), scope["order_id"])

    # 都不覆盖：有在期订单时报版本/功能层的问题，没有在期订单时报服务态问题。
    # 两条路径都取排序最优的那条订单的单订单判定原因，避免多原因并列让调用方难分支。
    pool = active or rows
    _, scope = min(pool, key=lambda pair: _order_rank(pair[0], now))
    return settle(scope["reason_code"], scope["reason"])


def describe_feature_scope(
    user_id: str,
    feature=None,
    now: Optional[float] = None,
) -> dict:
    """查询用户全部订单，判定某功能是否在服务范围内。

    与 describe_order 的分工：describe_order 判定单条订单（主订单或指定订单），
    本函数按多订单汇总判定，回答「这个功能现在能不能用」。
    取数与判定分离：判定逻辑在纯函数里，可脱离数据库单测。
    """
    return evaluate_feature_scope(list_orders(user_id), feature, now)
