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


class RenewalHintResponse(BaseModel):
    """一条订单的续费提醒：状态、级别与一句可展示的说明。

    state 是稳定标识（none / active / upcoming / due / not_started / blocked /
    unknown），remind 直接给出「要不要打扰用户」，前端不必自己按状态枚举判断；
    urgency 决定展示样式，suggested_action 给出下一步动作。
    """

    order_id: Optional[str] = None
    product_tier: str = ""
    tier_label: str = "未知版本"
    order_status: str = ""
    state: str = "none"
    state_label: str = "无订单"
    remind: bool = False
    urgency: str = "none"
    suggested_action: str = "none"
    window_days: int = 0
    days_remaining: int = 0
    days_overdue: int = 0
    expires_text: str = ""
    message: str = ""


class RenewalListView(BaseModel):
    """用户的续费提醒视图：逐单提醒 + 需处理的数量。

    due_count 与 upcoming_count 分开给：已到期需要立即处理，
    即将到期只需在列表里提示，两者混在一起会让用户每进一次页面就被弹窗。
    """

    has_order: bool = False
    order_count: int = 0
    remind_count: int = 0
    due_count: int = 0
    upcoming_count: int = 0
    window_days: int = 0
    hints: list[RenewalHintResponse] = Field(default_factory=list)
    message: str = ""


class ExpireResultResponse(BaseModel):
    """到期订单归档结果：本次处理了哪些、还剩多少。

    has_more 为真时调用方可再次调用，不必自己按 limit 推算剩余数量。
    """

    limit: int = 0
    scanned: int = 0
    remaining: int = 0
    has_more: bool = False
    archived: list[str] = Field(default_factory=list, description="本次归档为过期的订单号")
    skipped: list[str] = Field(default_factory=list, description="无需变更或不满足归档条件的订单号")
    message: str = ""


class FeatureScopeItemResponse(BaseModel):
    """功能判定里一条订单的结论摘要：版本、状态与该版本是否覆盖功能。

    feature_included 是版本层结论（该版本能力表里有没有这项功能），
    与服务期无关；能不能用还要叠加 in_service 一起看。
    """

    order_id: str
    tier_label: str = "未知版本"
    order_status: str = ""
    in_service: bool = False
    feature_included: bool = False
    reason_code: str = ""
    reason: str = ""


class FeatureCheckResponse(BaseModel):
    """功能服务范围判定响应：跨全部订单回答「这个功能能不能用」。

    与 OrderScopeResponse 的区别：那个只判定一条订单（主订单或指定订单），
    本模型按多订单汇总——任一在期订单覆盖该功能即判为在范围内，
    covered_by 给出具体是哪条订单覆盖的；orders 是逐单明细，
    供调用方解释「为什么不在范围内」（哪条到期、哪条退订）。
    """

    has_order: bool = False
    feature: FeatureScopeResponse
    in_scope: bool = False
    covered_by: Optional[str] = None
    order_count: int = 0
    in_service_count: int = 0
    reason_code: str = ""
    reason: str = ""
    message: str = ""
    orders: list[FeatureScopeItemResponse] = Field(default_factory=list)


class AdminOrderItemResponse(BaseModel):
    """管理侧订单列表里的一条记录：归属、版本、状态与服务期。

    与 OrderSummaryResponse 的区别是带上 user_id——管理侧要按客户排查，
    只给订单号无法回答「这个订单是谁的」。status_label 是订单状态中文名，
    in_service 才是叠加服务期后的「现在能不能用」，两者分开给，
    避免把「状态有效但已到期」的订单读成可用。
    """

    order_id: str
    user_id: str = ""
    product_tier: str = ""
    tier: Optional[str] = Field(default=None, description="归一后的版本：basic / pro")
    tier_label: str = "未知版本"
    status: str = ""
    status_label: str = "未知状态"
    period: Optional[ServicePeriodResponse] = None
    in_service: bool = False
    expires_text: str = ""
    days_remaining: int = 0


class AdminOrderCountResponse(BaseModel):
    """筛选项下的一组计数：状态或版本的取值、中文名与条数。

    计数覆盖全部命中筛选的订单，不受分页影响，前端据此展示分组标签；
    key 为原始取值（含未知兜底桶），label 可直接展示。
    """

    key: str
    label: str = ""
    count: int = 0


class AdminOrderListView(BaseModel):
    """管理侧订单列表：分页结果、生效的筛选条件与分组计数。

    筛选条件在响应里原样回显，调用方不必自己记录「这次查的是哪些条件」；
    by_status / by_tier 是全量计数，items 只是当前页，两者口径不同，
    前端翻页时分组标签不会跟着页码跳动。
    """

    items: list[AdminOrderItemResponse] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 0
    pages: int = 0
    has_more: bool = False
    order: str = "desc"
    statuses: list[str] = Field(default_factory=list, description="生效的状态筛选")
    tiers: list[str] = Field(default_factory=list, description="生效的版本筛选")
    user_id: str = ""
    keyword: str = ""
    by_status: list[AdminOrderCountResponse] = Field(default_factory=list)
    by_tier: list[AdminOrderCountResponse] = Field(default_factory=list)
    message: str = ""


class OrderStatusFlowResponse(BaseModel):
    """订单状态流转说明：当前状态、是否终态与可流转到的目标状态。

    与 models.order 的状态机同源：allowed_statuses 直接取合法流转表，
    前端据此渲染操作按钮，不必自己再维护一份流转表。
    known 为假表示存量数据里的状态值无法识别，此时不给任何可流转项。
    """

    order_id: Optional[str] = None
    status: str = ""
    status_label: str = "未知状态"
    known: bool = False
    terminal: bool = False
    allowed_statuses: list[str] = Field(default_factory=list)
    allowed_labels: list[str] = Field(default_factory=list)


class AdminOrderDetailResponse(BaseModel):
    """管理侧订单详情：一条订单的完整视图。

    与 AdminOrderItemResponse 的区别是把列表摘要展开为单条全量信息：
    产品能力、服务范围结论、状态流转去向、续费提示与该客户的订单总数，
    客服打开一条订单即可完成核对，不必再按接口逐个拼。
    allowed / reason_code / reason 是不带功能入参的整体服务结论，
    与 /order/scope 的口径一致。
    """

    order_id: str
    user_id: str = ""
    product_tier: str = ""
    tier: Optional[str] = Field(default=None, description="归一后的版本：basic / pro")
    tier_label: str = "未知版本"
    product_name: str = ""
    status: str = ""
    status_label: str = "未知状态"
    period: Optional[ServicePeriodResponse] = None
    in_service: bool = False
    expires_text: str = ""
    days_remaining: int = 0
    allowed: bool = False
    reason_code: str = ""
    reason: str = ""
    features: list[str] = Field(default_factory=list, description="该版本包含的功能名")
    user_order_count: int = Field(default=0, description="该客户名下的订单总数")
    status_flow: OrderStatusFlowResponse
    renewal: RenewalHintResponse
    message: str = ""

