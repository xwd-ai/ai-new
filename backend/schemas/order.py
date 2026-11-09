"""订单查询的请求与响应契约。

字段与 services.orders 的判定结论一一对应：接口层只负责取数与透传，
不在这里重新解释业务规则；响应模型显式声明对外字段，
服务层日后新增内部字段也不会意外漏给调用方。
"""

from typing import Optional

from pydantic import BaseModel, Field


class OrderQueryRequest(BaseModel):
    """订单查询入参：可指定一项功能，顺带判断它在当前版本内是否可用。

    feature 留空表示只判断整体服务状态（版本是否有效、是否在服务期内）；
    填了功能名则额外给出「当前版本是否包含该功能」的结论，
    功能名可以是标准名称、能力编码或用户口语（如「批量导出」「导出」「API」）。
    """

    feature: str = Field(
        default="", max_length=50, description="要确认的功能，留空只判断整体服务状态"
    )
    order_id: str = Field(
        default="", max_length=64, description="留空判定主订单，指定后只判定该订单"
    )


class ServicePeriodResponse(BaseModel):
    """服务期结论：状态、起止时间与天数口径。

    起止时间可能缺失（订单数据不完整），此时 status 为 unknown，
    started_at / expires_at 为 null，天数一律归零。
    """

    status: str = Field(description="active / not_started / expired / unknown")
    status_label: str = Field(description="状态中文名，可直接展示")
    started_at: Optional[float] = None
    expires_at: Optional[float] = None
    started_text: str = ""
    expires_text: str = ""
    days_total: int = 0
    days_used: int = 0
    days_remaining: int = 0
    percent_used: float = 0.0
    active: bool = False
    expired: bool = False
    not_started: bool = False


class FeatureScopeResponse(BaseModel):
    """功能判定结论：用户问的原文、解析出的能力与是否包含。

    解析不出的功能 code / name 为空字符串，included 为 false：
    「判断不了」不放行，由调用方提示用户核实。
    """

    query: str = Field(description="用户查询的功能原文")
    code: str = ""
    name: str = ""
    included: bool = False


class OrderScopeResponse(BaseModel):
    """订单服务范围响应：版本、服务期、功能范围与一句可展示的结论。

    reason_code 是判定用的稳定标识，前端按它分支；
    message 是拼好的中文说明，直接展示即可。
    """

    has_order: bool = False
    order_id: Optional[str] = None
    tier: Optional[str] = Field(default=None, description="归一后的版本：basic / pro")
    tier_label: str = "未知版本"
    order_status: str = ""
    period: Optional[ServicePeriodResponse] = None
    features: list[str] = Field(default_factory=list, description="当前版本包含的功能名")
    feature: Optional[FeatureScopeResponse] = None
    in_service: bool = False
    allowed: bool = False
    reason_code: str = ""
    reason: str = ""
    message: str = ""


class OrderSummaryResponse(BaseModel):
    """多订单列表里的一条订单摘要：订单号、版本、服务期与结论。

    与 OrderScopeResponse 的区别是不含功能判定——列表只回答
    「每个订单分别是什么状态」，逐条深入功能要看 /order/scope。
    """

    order_id: str
    product_tier: str = ""
    tier: Optional[str] = Field(default=None, description="归一后的版本：basic / pro")
    tier_label: str = "未知版本"
    order_status: str = ""
    period: Optional[ServicePeriodResponse] = None
    in_service: bool = False
    allowed: bool = False
    reason_code: str = ""
    reason: str = ""


class OrderListView(BaseModel):
    """用户的多订单视图：全部订单、主订单与在期版本的能力并集。

    primary_order_id 是「当前在用哪个版本」的单一答案，
    其余订单按到期时间从晚到早排列；features 是并集，
    用户同时持有多个在期版本时不会互相覆盖。
    """

    has_order: bool = False
    order_count: int = 0
    in_service_count: int = 0
    primary_order_id: Optional[str] = None
    orders: list[OrderSummaryResponse] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list, description="在期订单的能力并集")
    message: str = ""
