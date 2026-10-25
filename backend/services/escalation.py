"""转人工判定。

「要不要转人工」是一个业务判断，不应该只是一串关键词匹配——那样既不
可解释，也无法随服务规则调整。这里把它拆成几条明确的规则，每条规则
命中时留下原因，最终结果连同原因一起返回。

判定顺序体现业务优先级：
1. 用户明确要求转人工 —— 直接尊重。
2. 服务期外 —— 按服务规则，人工支持需要续费，先说明再转。
3. 没有订单记录 —— 无法判断服务范围，需要人工核实身份。
4. 超出支持范围（基础版无人工支持）—— 只能转人工。
5. 多轮无效 —— 用户反复反馈同一问题未解决，说明自助手段已用尽。

关键词只作为「用户表达了不满或求助」的辅助信号，不单独构成转人工理由。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 用户明确要求人工的信号
_EXPLICIT_HUMAN = ("转人工", "人工客服", "找人工", "真人客服", "投诉")

# 用户表达「问题仍未解决」的信号
_UNRESOLVED = ("还是不行", "还是没用", "没解决", "解决不了", "试了没用", "不行", "无效")


@dataclass
class EscalationDecision:
    """一次转人工判定的结果。"""

    suggest: bool
    reason: str = ""
    # 命中的规则名，便于日志与前端提示
    rules: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"suggest": self.suggest, "reason": self.reason, "rules": list(self.rules)}


def decide_escalation(
    message: str,
    *,
    order: dict | None = None,
    repeated_unresolved: bool = False,
) -> EscalationDecision:
    """判断是否建议转人工。

    ``order`` 为用户订单信息（无订单时为 None 或 has_order=False）。
    ``repeated_unresolved`` 表示用户已多次反馈问题未解决。
    """
    text = message or ""
    rules: list[str] = []

    # 1. 明确要求人工
    if any(k in text for k in _EXPLICIT_HUMAN):
        return EscalationDecision(
            suggest=True,
            reason="您希望与人工客服沟通，我来为您转接",
            rules=["explicit_request"],
        )

    # 2. 服务期外
    if order and order.get("has_order") and not order.get("in_service"):
        return EscalationDecision(
            suggest=True,
            reason="当前已超出服务期，人工支持需要续费后开通，可为您转人工协助处理",
            rules=["out_of_service"],
        )

    # 3. 无订单记录
    if not order or not order.get("has_order"):
        rules.append("no_order_record")
        return EscalationDecision(
            suggest=True,
            reason="未查询到订单信息，需要人工核实您的购买情况",
            rules=rules,
        )

    # 4. 超出支持范围：基础版仅社区支持
    tier = (order.get("product_tier") or "").lower()
    if tier and tier != "pro":
        return EscalationDecision(
            suggest=True,
            reason="当前版本不包含人工客服支持，可为您转人工进一步确认",
            rules=["tier_not_supported"],
        )

    # 5. 多轮未解决
    if repeated_unresolved or any(k in text for k in _UNRESOLVED):
        return EscalationDecision(
            suggest=True,
            reason="自助排查未能解决，建议转人工继续跟进",
            rules=["unresolved"],
        )

    return EscalationDecision(suggest=False)
