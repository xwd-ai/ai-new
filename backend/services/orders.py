"""订单与服务范围判定。

三层递进，后一层复用前一层的结论：

1. 版本能力矩阵：把「买的是哪个版本」翻译成一组能力编码。编码用「域.动作」
   的形式，改中文文案不影响判定，日志与落库也不会因为换个说法就对不上；
2. 服务期：把起止时间戳收敛成有效 / 未开始 / 已过期三种状态，附带总天数、
   剩余天数与已用比例，供话术与续费提示直接使用；
3. 综合判定：版本 + 服务期 + 具体功能，给出「能否按已购服务处理」的结论，
   以及一条可直接展示给用户的原因。

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


# ============================ 四、查询入口 ============================


def _get_product(db, tier: str) -> Optional[dict]:
    product = db.query(Product).filter(Product.id == tier).first()
    return product.to_dict() if product else None


def get_order(user_id: str) -> Optional[dict]:
    """取用户当前订单记录，没有订单返回 None。

    同一用户存在多条订单时取第一条，多订单的挑选口径在后续接口层处理。
    """
    db = SessionLocal()
    try:
        order = db.query(Order).filter(Order.user_id == user_id).first()
        return order.to_dict() if order else None
    finally:
        db.close()


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


def describe_order(user_id: str, feature=None, now: Optional[float] = None) -> dict:
    """查询用户订单，返回版本、服务期与功能范围的完整结论。

    与 query_order 的分工：query_order 面向页面展示，字段固定为既有契约；
    本函数面向「能不能用」的判断，把判定过程一并透出——
    版本、服务期、功能是否包含、原因码与一句可直接展示的说明。

    先取订单再交给纯函数判定，取数与判定分开，判定逻辑可脱离数据库单测。
    """
    scope = evaluate_service_scope(get_order(user_id), feature, now)
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
