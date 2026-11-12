# 订单查询工具

本文描述订单模块提供给对话链路的只读查询工具 `query_order`：怎么注册、怎么调用、返回什么、失败怎么区分。订单的 HTTP 接口见 [tickets-api.md](tickets-api.md#订单)，认证接口见 [auth-api.md](auth-api.md)，对话与知识库接口见 [api.md](api.md)。

工具实现位于 `backend/services/orders.py` 第九节（工具定义与执行）与第十节（注册与调用约定）。

## 与 HTTP 接口的分工

同一个判定逻辑有两种出口：接口给页面用，工具给模型用。

| 出口 | 面向对象 | 身份来源 | 返回形态 |
|---|---|---|---|
| `GET /api/order` | 工单页、转人工判定 | 登录态，未登录返回空订单 | 固定 7 键，单条主订单 |
| `GET /api/order/scope` | 页面上的「能不能用」判断 | 登录态，未登录 401 | 单条订单的完整判定过程 |
| `GET /api/order/list` | 多产品用户的订单列表 | 登录态，未登录 401 | 逐条摘要 + 能力并集 |
| 工具 `query_order` | 对话链路里的模型 | 注册时绑定的登录态 | `ToolResult`，按入参自动分派三种形态 |

工具不新增判定规则，只是把 `describe_order` / `describe_feature_scope` / `query_orders` 三个已有函数包成模型可调用的形状。判定口径与接口完全一致，改规则只需改这三个函数。

## 工具定义

| 项 | 值 |
|---|---|
| 名称 | `query_order` |
| 说明 | 查询当前登录用户的订单信息：购买的版本、服务期状态与可用功能。需要确认某个功能能不能使用时，把功能名称放在 `feature` 里。 |
| 只读 | 是。名字在 `backend/services/tools.py` 的 `READ_ONLY_TOOLS` 白名单内，`read_only` 与 `concurrent` 均由白名单推导，不靠注册时手写 |
| 参数 | 两个，都可省略 |

参数结构：

```json
{
  "type": "object",
  "properties": {
    "order_id": {"type": "string", "description": "订单号，留空则查询主订单（当前在用的那一条）"},
    "feature": {"type": "string", "description": "要确认的功能名称，留空则只判断整体服务状态"}
  },
  "required": [],
  "additionalProperties": false
}
```

| 参数 | 上限 | 说明 |
|---|---|---|
| `order_id` | 64 字符 | 只判定这一条订单。订单号不属于当前用户时按「查不到」处理，不因为是合法格式就放行 |
| `feature` | 50 字符 | 跨全部订单判断该功能是否在服务范围内，不受主订单版本影响 |

两个参数在长度上限、空白归一口径上与接口层一致：模型偶尔会把用户原话连着换行或多余空格塞进来，这类输入按同一个查询条件处理，不做二次切分。

**身份不是参数**。`user_id` 由调用方从登录态注入，刻意不写进参数表——一旦出现在参数里，模型就能自己指定要查谁的订单。执行时若参数里夹带了 `user_id` / `user` 这类身份键，会被直接丢弃。

## 注册与执行

```python
from backend.services import orders, tools

registry = tools.ToolRegistry()
spec = orders.register_order_tool(registry, user_id)      # 每轮对话装配工具时调用一次
result = registry.execute(orders.ORDER_TOOL_NAME, feature="批量导出")
```

注册返回注册表里的工具定义（`ToolSpec`），名字、说明、处理器一次对齐，调用方不必再手写一遍 `"query_order"`。调用约定：

- **注册时机**：每轮对话构建 `ToolRegistry` 时调用一次，登录身份在此时定死。匿名会话也可以注册，执行时返回「未登录」的失败结果，不需要在装配阶段分支判断。
- **执行方式**：`registry.execute("query_order", **arguments)`，`arguments` 按上表收 `order_id` / `feature`，未识别字段忽略。工具协议演进时多传字段不会让旧实现报错。
- **结果形态**：`ToolResult.success` 只表示查询是否可执行，`output` 是单行中文摘要，`data` 带 `tool` / `mode` / `order` 三段，可直接序列化进事件流。
- **定义复用**：提示词拼装用 `orders.order_tool_definition()`，与注册项同名同描述；返回的是副本，调用方改它不会污染模块常量。
- **并发**：工具名在只读白名单内，`plan_execution` 会把它与其它只读调用编进同一批并发执行。

注册表同名先注册者胜。对话侧目前仍保留一份旧的 `query_order` 注册（`backend/services/chat.py`），直接调用 `register_order_tool` 不会覆盖它；接入时应先撤掉旧注册再调用本函数，否则新工具只是「注册成功但永远不会被选中」。

## 结果形态

`ToolResult` 字段：

| 字段 | 说明 |
|---|---|
| `success` | 查询是否可执行。参数不可用为 `false`，查不到订单仍为 `true` |
| `output` | 单行摘要，取判定结论里的 `message`，可直接展示或拼进提示词 |
| `data` | `{"tool": "query_order", "mode": "...", "order": {...}}` |
| `error` | 失败时的中文原因，成功为空串 |
| `error_code` | 失败时的机器可读码，成功为空串 |
| `retryable` | 由注册表统一异常处理填写，本工具自身不产生可重试错误 |

`mode` 说明这次走了哪条查询路径：

| mode | 触发条件 | 底层函数 |
|---|---|---|
| `feature` | 传了 `feature` | `describe_feature_scope` |
| `order` | 未传 `feature`，传了 `order_id` | `describe_order` |
| `list` | 两者都没传 | `query_orders` |

功能名优先于订单号：两个都给时按 `feature` 跨订单判定，因为用户问「这个功能能不能用」时，他关心的是结论而不是某条订单。

### mode = feature

```json
{
  "tool": "query_order",
  "mode": "feature",
  "order": {
    "has_order": true,
    "feature": {"query": "批量导出", "code": "export.batch", "name": "批量导出", "included": true},
    "in_scope": true,
    "covered_by": "O-1001",
    "order_count": 1,
    "in_service_count": 1,
    "reason_code": "in_scope",
    "reason": "「批量导出」在服务范围内（专业版，服务期至 2027-04-12，剩余 120 天）",
    "message": "「批量导出」在服务范围内（专业版，服务期至 2027-04-12，剩余 120 天）",
    "orders": [
      {"order_id": "O-1001", "tier_label": "专业版", "order_status": "active",
       "in_service": true, "feature_included": true, "reason_code": "in_service", "reason": "在服务范围内"}
    ]
  }
}
```

`included` 表示「版本层是否含该功能」，`in_scope` 表示「功能可用」——后者还要求订单在期。`covered_by` 只在不覆盖时为空，多个在期订单覆盖时取排序最优的一条。`feature` 未传时不会走这个模式，因此不存在 `feature_required` 的空判。

### mode = order

```json
{
  "tool": "query_order",
  "mode": "order",
  "order": {
    "has_order": true,
    "order_id": "O-1001",
    "tier": "pro",
    "tier_label": "专业版",
    "order_status": "active",
    "period": {"status": "active", "status_label": "服务中", "started_at": 1790000000.0,
               "expires_at": 1807764610.1, "started_text": "2026-09-21", "expires_text": "2027-04-12",
               "days_total": 203, "days_used": 82, "days_remaining": 120, "percent_used": 40.4,
               "active": true, "expired": false, "not_started": false},
    "features": ["基础导出", "单文件上传", "社区支持", "批量导出", "高级报表", "优先客服支持", "API 接口"],
    "feature": null,
    "in_service": true,
    "allowed": true,
    "reason_code": "in_service",
    "reason": "在服务范围内",
    "message": "专业版服务中，服务期至 2027-04-12（剩余 120 天）"
  }
}
```

订单号不存在或不属于当前用户时，返回同样的 13 键，只是 `has_order` 为 `false`、`reason_code` 为 `no_order`，不抛错。

### mode = list

```json
{
  "tool": "query_order",
  "mode": "list",
  "order": {
    "has_order": true,
    "order_count": 2,
    "in_service_count": 1,
    "primary_order_id": "O-1002",
    "orders": [
      {"order_id": "O-1001", "product_tier": "basic", "tier": "basic", "tier_label": "基础版",
       "order_status": "expired", "period": {}, "in_service": false, "allowed": false,
       "reason_code": "expired", "reason": "服务期已结束"},
      {"order_id": "O-1002", "product_tier": "pro", "tier": "pro", "tier_label": "专业版",
       "order_status": "active", "period": {}, "in_service": true, "allowed": true,
       "reason_code": "in_service", "reason": "在服务范围内"}
    ],
    "features": ["基础导出", "单文件上传", "社区支持", "批量导出", "高级报表", "优先客服支持", "API 接口"],
    "message": "共 2 个订单，1 个在服务期内"
  }
}
```

`features` 只取在期订单的能力并集：基础版和专业版都在期时不会因为主订单挑中基础版就把批量导出判成不可用。`orders` 里每条都带 `period` 与原因码，便于逐条说明「哪一条到期了」。

无订单时返回 `has_order=false`、`order_count=0`、`orders=[]`、`features=[]`。

## 失败与空结果

两者必须分开看：**失败是「这次调用问不出东西」，空结果是「问出来了，答案是这个人没订单」**。

| 情形 | success | error_code | 说明 |
|---|---|---|---|
| 身份为空或 `anonymous` | `false` | `order_tool_no_user` | 匿名会话没有可查订单，按未登录处理 |
| `feature` 超过 50 字符 | `false` | `order_tool_invalid_parameter` | 不带着明显不合法的输入去数据库里白跑一次 |
| `order_id` 超过 64 字符 | `false` | `order_tool_invalid_parameter` | 同上 |
| 用户没有订单 | `true` | 空 | `mode=list`、`has_order=false` |
| 订单号不存在 / 不属于本人 | `true` | 空 | `mode=order`、`has_order=false`、`reason_code=no_order` |
| 工具未注册 | `false` | 空 | `registry.execute` 返回「工具不存在」，`error` 带工具名 |
| 处理器内部异常 | `false` | 异常分类码 | 由 `ToolRegistry.execute` 统一转换，不让异常冒到对话循环 |

查不到订单刻意不算失败：模型需要知道「这个人没有订单」才能继续回答，把它抛成异常只会让对话停在一句无关的工具错误上。

对话侧据 `error_code` 分支：`order_tool_no_user` 引导登录，`order_tool_invalid_parameter` 让模型改写查询条件（例如把整段话收窄成一个功能名），其余按通用工具故障处理。

## 并发与顺序

`query_order` 在只读白名单内，可以和知识检索同时发起。`plan_execution` 按「只读并发、写操作屏障」切批（`create_ticket` 只是示意用的写操作，当前未注册）：

```python
plan_execution(["search_knowledge", "query_order"])        # → [["search_knowledge", "query_order"]]
plan_execution(["search_knowledge", "query_order", "create_ticket"])
# → [["search_knowledge", "query_order"], ["create_ticket"]]
```

写操作前后的只读调用各自成组，保证读到的是写之前或写之后的一致状态。未知工具一律按写操作处理，宁可慢一点也不误判并发。因此工具改名时必须同步白名单：名字对不上就会被当成有副作用的工具，只读查询白白退化成串行。

## 边界

- **只读**：不支持改状态、续费、建单。管理员的订单维护走接口，不给模型开口子。
- **不跨用户**：任何参数组合都只能看到注册时绑定的那个人的订单。管理员身份也一样，工具不做全量查询。
- **不接收时间基准**：`now` 由执行时取当前时间，不作为参数暴露，避免模型用错误的时间点把在期订单判成过期。
- **功能名走包含匹配**：先精确匹配编码与全名，再做单向最长包含，「导出」不会被「批量导出」抢走；识别不出时返回 `feature_unknown`，由模型追问用户具体指哪个功能。
- **不做缓存**：每次执行都回库取数，订单状态变更后立刻生效。

## 接入检查清单

- [ ] 每轮对话构建注册表时调用一次 `register_order_tool(registry, user_id)`，不要复用上一轮的身份。
- [ ] 撤掉对话侧原有的同名注册，确认 `registry.get("query_order")` 拿到的是新处理器。
- [ ] 提示词里的工具说明取自 `order_tool_definition()`，不要另写一份文案。
- [ ] 按 `error_code` 分支处理失败，不要只看 `success`。
- [ ] 不把 `user_id` 塞进参数（塞了也会被丢弃，但会让定义与实现不一致）。
- [ ] 结果进事件流前只取 `output` 与 `data`，不要把整个 `ToolResult` 序列化给前端。
