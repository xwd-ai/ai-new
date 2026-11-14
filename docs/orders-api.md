# 订单 API

本文描述订单（模块 2）相关接口的请求、响应与错误码。认证接口见 [auth-api.md](auth-api.md)，工单接口见 [tickets-api.md](tickets-api.md)，知识库、对话与健康检查接口见 [api.md](api.md)。同一套判定逻辑还以只读工具 `query_order` 的形式供对话链路调用，见 [订单工具化说明](order-tool.md)。

服务默认地址为 `http://127.0.0.1:8000`，所有接口前缀为 `/api`，请求与响应均为 JSON。也可在 [Swagger UI](http://127.0.0.1:8000/docs) 中查看自动生成的 OpenAPI 结构。

## 接口清单

| 接口 | 权限 | 说明 |
|---|---|---|
| `GET /api/order` | 可匿名 | 当前用户的主订单与服务范围，供页面展示 |
| `GET /api/order/list` | 需登录 | 当前用户的全部订单、主订单与在期能力并集 |
| `GET /api/order/scope` | 需登录 | 单条订单的服务范围完整判定（可指定功能与订单号） |
| `GET /api/order/feature-scope` | 需登录 | 跨全部订单判定某功能是否在服务范围内 |
| `GET /api/order/renewal` | 需登录 | 当前用户各订单的续费提醒 |
| `GET /api/order/admin` | 管理员 | 跨客户订单列表（筛选 / 分页 / 分组计数） |
| `GET /api/order/admin/{order_id}` | 管理员 | 单条订单的完整详情 |
| `POST /api/order/expire` | 管理员 | 把服务期已结束的有效订单归档为过期 |

演示账号为 `admin / admin123`（管理员）与 `customer / customer123`（客户）。

## 权限约定

- `/api/order` 是唯一匿名友好的订单接口：未登录返回 `{"order": null}` 而不是 401，前端据此引导登录，而不是把「还没登录」渲染成接口异常。
- 其余面向用户的接口（`/list` `/scope` `/feature-scope` `/renewal`）要求登录，未登录返回 401。`/order/list` 不做「返回空列表」的宽松处理——空列表与「确实没有订单」无法区分，调用方容易把未登录当成没买过东西。
- 管理侧接口（`/admin`、`/admin/{order_id}`、`/expire`）要求管理员身份，普通用户返回 403。
- `/order/scope` 的 `order_id` 只在当前用户名下生效：指定他人订单号或不存在的订单号，都按「查不到」处理，返回 `no_order` 结论，不因为是合法格式就放行。

## 错误响应

订单接口与工单接口挂在同一路由下，业务失败统一返回以下结构，HTTP 状态码与 `error_code` 同时给出：

```json
{"error":"订单不存在","error_code":"ORDER_NOT_FOUND","error_detail":{"order_id":"O-9999"}}
```

- `error`：可直接展示的中文文案，措辞可能调整。
- `error_code`：机器可读标识，调用方应以此分支处理。
- `error_detail`：可选，只在有补充上下文时出现，例如出问题的筛选字段名与取值。

请求参数的结构校验失败（类型错误、超出长度上限）由框架先行拦截，返回的是另一种结构，`detail` 为错误数组：

```json
{"detail":[{"type":"string_too_long","loc":["query","feature"],"msg":"String should have at most 50 characters","input":"xxx…"}]}
```

客户端需同时处理这两种 400/422 形态。

### 错误码对照

| error_code | HTTP | 触发场景 |
|---|---|---|
| `ORDER_NOT_FOUND` | 404 | 管理侧详情按订单号查不到该订单 |
| `ORDER_INVALID_FILTER` | 400 | 管理侧列表的筛选或分页入参不可用：未知状态、未知版本、客户编号超长、关键词超长、页码或每页条数非法、排序方向不是 asc/desc |
| `TICKET_UNAUTHORIZED` | 401 | 未登录或访问令牌无效 |
| `TICKET_FORBIDDEN` | 403 | 普通用户访问管理接口 |

未登录与无权限的错误码沿用工单模块的前缀（`TICKET_*`）：订单接口与工单接口共用同一套路由与权限依赖，对外错误码保持一套，前端不必按模块区分登录失败的分支。

另有 `ORDER_UNKNOWN_STATUS` / `ORDER_INVALID_TRANSITION` 两个错误码属于订单状态流转校验（服务层，供人工改状态与到期归档共用），当前 HTTP 接口不直接返回。

## 订单状态与服务期

两个维度相互独立，要分别判断，不能互相推导：

**订单状态**（`order_status` / `status`，取值见 `OrderStatus`）：

| 值 | 中文名 | 说明 |
|---|---|---|
| `active` | 有效 | 服务按服务期继续提供 |
| `expired` | 过期 | 服务期结束且未续费，归档而来 |
| `cancelled` | 退订 | 用户主动退订，服务立即停止，是终态 |

合法流转：有效 → 过期 / 退订；过期 → 有效（续费恢复）/ 退订。退订后不再流转，重新购买应生成新订单。

**服务期状态**（`period.status`）：

| 值 | 中文名 | 说明 |
|---|---|---|
| `active` | 服务中 | 开始时间 ≤ 当前 < 到期时间（左闭右开） |
| `not_started` | 未开始 | 当前 < 开始时间 |
| `expired` | 已到期 | 当前 ≥ 到期时间 |
| `unknown` | 无法判定 | 起止时间缺失或颠倒，交给人工核实 |

`in_service` 要求订单状态为 `active` **且** 服务期为 `active`：状态有效但已到期的订单不算在服务中，反之亦然。天数口径：已用天数向下取整、剩余天数向上取整（不足一天也算还有一天）。

### 服务范围原因码

`/order/scope` 与 `/order/feature-scope` 的 `reason_code` 按业务优先级逐层收紧，命中即停——先报最上游的问题，用户才知道该先解决哪一步：

| reason_code | 含义 |
|---|---|
| `no_order` | 未查询到订单记录，无法判断服务范围 |
| `tier_unknown` | 无法识别所购版本，需要人工核实购买情况 |
| `order_inactive` | 订单当前不是有效状态，服务已停止 |
| `not_started` | 服务期尚未开始 |
| `expired` | 服务期已结束 |
| `period_unknown` | 订单起止时间不完整，无法判断服务期 |
| `feature_unknown` | 无法识别要确认的功能 |
| `feature_not_included` | 当前版本不包含该功能 |
| `in_service` | 在服务范围内 |
| `feature_required` | 未提供要确认的功能名称（仅 `/order/feature-scope`） |
| `in_scope` | 功能在服务范围内（仅 `/order/feature-scope`） |

## 订单概览

`GET /api/order` 返回当前登录用户的主订单与服务范围，供工单页右侧面板展示当前版本。未登录或令牌无效时返回 `{"order": null}`。

```json
{
  "order": {
    "has_order": true,
    "order_id": "O-1001",
    "product_tier": "pro",
    "product_name": "专业版",
    "in_service": true,
    "features": ["批量导出","高级报表","优先客服支持","API 接口"],
    "expires_at": 1807764610.1
  }
}
```

响应字段固定为以上 7 个键。无订单时为 `{"order": {"has_order": false, "message": "未查询到该用户的订单记录"}}`。`expires_at` 为 Unix 时间戳。

与 `/order/list` 的分工：本接口只给一条主订单（当前在用的那一条），字段固定；多产品用户要看逐条订单时用 `/order/list`。

## 订单列表

`GET /api/order/list` 列出当前用户的全部订单，并标出主订单与在期版本的能力并集。用户同时持有多个在期订单时，可用功能取并集，不会因为主订单挑中基础版就把专业版功能判成不可用。

```json
{
  "has_order": true,
  "order_count": 1,
  "in_service_count": 1,
  "primary_order_id": "O-1001",
  "orders": [
    {
      "order_id": "O-1001",
      "product_tier": "pro",
      "tier": "pro",
      "tier_label": "专业版",
      "order_status": "active",
      "period": {
        "status": "active",
        "status_label": "服务中",
        "started_at": 1784436610.1,
        "expires_at": 1807764610.1,
        "started_text": "2026-07-19",
        "expires_text": "2027-04-15",
        "days_total": 270,
        "days_used": 118,
        "days_remaining": 152,
        "percent_used": 43.7,
        "active": true,
        "expired": false,
        "not_started": false
      },
      "in_service": true,
      "allowed": true,
      "reason_code": "in_service",
      "reason": "在服务范围内"
    }
  ],
  "features": ["批量导出","高级报表","优先客服支持","API 接口"],
  "message": "共 1 个订单，均在服务期内"
}
```

- `orders` 里的每条是摘要（不含功能判定），按到期时间从晚到早排列；逐条深入功能判定看 `/order/scope`。
- `primary_order_id` 是「当前在用哪个版本」的单一答案。
- `features` 是全部在期订单的能力并集。
- `message` 摘要随在期数量变化：「共 N 个订单，均在服务期内」「共 N 个订单，M 个在服务期内」「共 N 个订单，当前均不在服务期内」；无订单时 `has_order` 为 `false`，`message` 为「未查询到该用户的订单记录」。

## 服务范围判定

`GET /api/order/scope` 回答「能不能用」：把判定结论、原因码与一句可展示的说明一次给全。查询参数：

| 参数 | 上限 | 说明 |
|---|---|---|
| `feature` | 50 字符 | 要确认的功能，留空只判断整体服务状态。可以是标准名称、能力编码或用户口语（如「批量导出」「API」） |
| `order_id` | 64 字符 | 指定后只判定该订单；留空判定主订单 |

```json
{
  "has_order": true,
  "order_id": "O-1001",
  "tier": "pro",
  "tier_label": "专业版",
  "order_status": "active",
  "period": { "status": "active", "status_label": "服务中", "…": "同上" },
  "features": ["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],
  "feature": {"query": "批量导出", "code": "export.batch", "name": "批量导出", "included": true},
  "in_service": true,
  "allowed": true,
  "reason_code": "in_service",
  "reason": "在服务范围内",
  "message": "专业版包含「批量导出」，服务期至 2027-04-15（剩余 152 天）"
}
```

- `allowed` 是最终结论：版本可识别、服务在期内、功能在版本内，三者同时满足才放行。
- `feature.included` 是「版本层是否含该功能」，与服务期无关。解析不出的功能 `code` / `name` 为空、`included` 为 `false`——「判断不了」不放行。
- 未给 `feature` 时 `feature` 为 `null`，只看整体服务状态。
- `message` 在结论为 `in_service` 时补上到期时间与剩余天数；其余情况沿用 `reason`。例如 `feature_not_included` 的 `reason` 为「专业版不包含该功能「私有化部署」」形态，`expired` 为「服务期已结束（2027-04-15）」形态。

## 功能范围判定

`GET /api/order/feature-scope` 跨全部订单回答「这个功能现在能不能用」：任一在期订单的版本覆盖该功能即判为在范围内。与 `/order/scope` 的分工：那个只判定单条订单，多产品用户「主订单是基础版、另有一条在期专业版」时会被主订单带偏。查询参数 `feature`（上限 50 字符）。

```json
{
  "has_order": true,
  "feature": {"query": "批量导出", "code": "export.batch", "name": "批量导出", "included": true},
  "in_scope": true,
  "covered_by": "O-1001",
  "order_count": 1,
  "in_service_count": 1,
  "reason_code": "in_scope",
  "reason": "「批量导出」在服务范围内（专业版，服务期至 2027-04-15，剩余 152 天）",
  "message": "「批量导出」在服务范围内（专业版，服务期至 2027-04-15，剩余 152 天）",
  "orders": [
    {"order_id": "O-1001", "tier_label": "专业版", "order_status": "active", "in_service": true, "feature_included": true, "reason_code": "in_service", "reason": "在服务范围内"}
  ]
}
```

- `covered_by` 是覆盖该功能的订单号，`in_scope` 为真时才有值。
- `orders` 是逐单明细，供调用方解释「为什么不在范围内」（哪条到期、哪条退订、哪条版本不包含）。
- 不在范围内时只给一个原因码（取排序最优订单的单订单原因），不把多条原因并列：
  - 未给功能 → `feature_required`（「请提供要确认的功能名称」）；
  - 功能解析不出 → `feature_unknown`（「无法识别要确认的功能「xxx」」）；
  - 有在期订单但都不包含 → `feature_not_included` / `tier_unknown`；
  - 没有在期订单 → `not_started` / `expired` / `order_inactive` 等服务态原因。
- `feature.included` 是版本层并集结论（任一订单的版本含该功能即为 `true`），与 `in_scope` 可以不一致：例如专业版订单已到期时 `included` 为 `true` 但 `in_scope` 为 `false`。

## 续费提醒

`GET /api/order/renewal` 把服务期换算成提醒状态、级别与建议动作。查询参数 `window_days`（0–365，默认 30）：提前多少天开始提示续费。已退订订单不提醒续费只提示重新购买，服务期未开始的订单不提醒。

```json
{
  "has_order": true,
  "order_count": 1,
  "remind_count": 0,
  "due_count": 0,
  "upcoming_count": 0,
  "window_days": 30,
  "hints": [
    {
      "order_id": "O-1001",
      "product_tier": "pro",
      "tier_label": "专业版",
      "order_status": "active",
      "state": "active",
      "state_label": "服务中",
      "remind": false,
      "urgency": "none",
      "suggested_action": "none",
      "window_days": 30,
      "days_remaining": 152,
      "days_overdue": 0,
      "expires_text": "2027-04-15",
      "message": "服务中，服务期至 2027-04-15（剩余 152 天）"
    }
  ],
  "message": "共 1 个订单，暂无需续费"
}
```

**提醒状态**（`state`）：

| 值 | 中文名 | remind | 说明 |
|---|---|---|---|
| `none` | 无订单 | 否 | 压根没有订单 |
| `active` | 服务中 | 否 | 有订单且离续费还早 |
| `upcoming` | 即将到期 | 是 | 剩余天数落入提醒窗口 |
| `due` | 已到期 | 是 | 服务期已过或已归档为过期，需立即续费 |
| `not_started` | 未开始 | 否 | 服务还没开始，暂无需续费 |
| `blocked` | 已退订 | 否 | 不再提供服务，引导重新购买 |
| `unknown` | 无法判定 | 否 | 状态或起止时间无法识别，需人工核实 |

**紧迫级别**（`urgency`）：`none` 不打扰 / `info` 列表提示 / `warning` 建议提前续费（剩余 ≤ `window_days`）/ `critical` 尽快续费（剩余 ≤ 7 天或已到期）。

**建议动作**（`suggested_action`）：`renew` 续费 / `wait` 等待生效 / `buy_new` 重新购买 / `verify` 人工核实 / `none` 无需动作。

`remind_count` = `due_count` + `upcoming_count`，两者分开给：已到期需要立即处理（可弹窗），即将到期留在列表里提示即可。`message` 摘要按「已到期 / 即将到期 / 暂无需续费」给出，两类都有时形如「N 个订单已到期，另有 M 个将在 30 天内到期」。

## 管理侧订单列表

`GET /api/order/admin` 跨客户查询订单，供客服按状态找待处理订单、按客户查历史购买、按版本核对权益范围。查询参数：

| 参数 | 上限 | 说明 |
|---|---|---|
| `status` | — | 按订单状态筛选，支持单状态、逗号分隔多状态与重复传参，`all` 或不传不筛选 |
| `tier` | — | 按产品版本筛选（`basic` / `pro` 及中文别名），支持多选 |
| `user_id` | 64 字符 | 按客户编号精确筛选 |
| `keyword` | 64 字符 | 关键词，模糊匹配订单号 / 客户编号（`%` `_` 按普通字符匹配） |
| `page` | — | 页码，从 1 开始 |
| `page_size` | — | 每页条数，默认 20，上限 100 |
| `order` | — | 按到期时间排序：`asc` 正序 / `desc` 逆序（默认） |

未知状态或未知版本直接返回 400，不静默给空列表——空列表说明不了是「真没有」还是「条件写错了」。各筛选项之间是「与」关系。

```json
{
  "items": [
    {
      "order_id": "O-1001",
      "user_id": "u-customer",
      "product_tier": "pro",
      "tier": "pro",
      "tier_label": "专业版",
      "status": "active",
      "status_label": "有效",
      "period": { "status": "active", "status_label": "服务中", "…": "同上" },
      "in_service": true,
      "expires_text": "2027-04-15",
      "days_remaining": 152
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 5,
  "pages": 1,
  "has_more": false,
  "order": "desc",
  "statuses": [],
  "tiers": [],
  "user_id": "",
  "keyword": "",
  "by_status": [
    {"key": "active", "label": "有效", "count": 1},
    {"key": "expired", "label": "过期", "count": 0},
    {"key": "cancelled", "label": "退订", "count": 0}
  ],
  "by_tier": [
    {"key": "basic", "label": "基础版", "count": 0},
    {"key": "pro", "label": "专业版", "count": 1}
  ],
  "message": "共 1 个订单：有效 1 个"
}
```

- 筛选条件在响应里原样回显（`statuses` / `tiers` / `user_id` / `keyword`），调用方不必自己记录「这次查的是哪些条件」。
- `by_status` / `by_tier` 是全量计数（覆盖全部命中筛选的订单，不受分页影响），`items` 只是当前页，翻页时分组标签不会跟着跳动。已知状态即使一条都没有也返回 0，标签位置稳定；历史脏数据里的未知状态 / 未知版本单独成桶排在末尾。
- `status` 与 `in_service` 分开给：状态有效但服务期已过的订单在列表里很常见，只按状态展示会让人误以为还能用。
- `message` 形如「共 N 个订单：有效 X 个、过期 Y 个」；带筛选时为「按 状态 有效 + 关键词「O-1001」 筛选到 N 个订单：…」；多页时追加「，当前第 p/P 页」。

## 管理侧订单详情

`GET /api/order/admin/{order_id}` 按订单号查看任意客户的单条订单完整信息：产品能力、服务范围结论、状态流转去向、续费提示与该客户的订单总数，客服打开一条订单即可完成核对。订单不存在返回 404（`ORDER_NOT_FOUND`）。

```json
{
  "order_id": "O-1001",
  "user_id": "u-customer",
  "product_tier": "pro",
  "tier": "pro",
  "tier_label": "专业版",
  "product_name": "专业版",
  "status": "active",
  "status_label": "有效",
  "period": { "status": "active", "status_label": "服务中", "…": "同上" },
  "in_service": true,
  "expires_text": "2027-04-15",
  "days_remaining": 152,
  "allowed": true,
  "reason_code": "in_service",
  "reason": "在服务范围内",
  "features": ["批量导出","高级报表","优先客服支持","API 接口"],
  "user_order_count": 1,
  "status_flow": {
    "order_id": "O-1001",
    "status": "active",
    "status_label": "有效",
    "known": true,
    "terminal": false,
    "allowed_statuses": ["expired", "cancelled"],
    "allowed_labels": ["过期", "退订"]
  },
  "renewal": { "…": "同续费提醒的单条结构" },
  "message": "订单 O-1001（客户 u-customer）：专业版服务中，服务期至 2027-04-15（剩余 152 天）；当前状态有效，可流转到过期、退订"
}
```

- `allowed` / `reason_code` / `reason` 是不带功能入参的整体服务结论，与 `/order/scope` 的口径一致。
- `status_flow` 直接取状态机的合法流转表，前端据此渲染操作按钮，不必自己再维护一份流转表。`known` 为假表示存量数据里的状态值无法识别，此时不给任何可流转项；终态（退订）的 `allowed_statuses` 为空。
- `renewal` 与 `/order/renewal` 的单条结构相同。

## 到期归档

`POST /api/order/expire` 把服务期已结束、状态仍是有效的订单归档为过期。候选订单按到期时间从早到晚处理，每条都走订单状态机，退订订单与已过期订单不会被重复改动。查询参数 `limit`（1–500，默认 100）：单次最多归档的订单数。

```json
{
  "limit": 10,
  "scanned": 0,
  "remaining": 0,
  "has_more": false,
  "archived": [],
  "skipped": [],
  "message": "没有需要归档的到期订单"
}
```

- `archived` 是本次归档为过期的订单号，`skipped` 是无需变更或不满足归档条件的订单号。
- `has_more` 为真时可再次调用，不必自己按 `limit` 推算剩余数量。
- 归档只改订单状态，不动服务期时间：到期时间属于合同事实，续费是把订单流转回有效或延长服务期，两者不混在一次操作里。

## PowerShell 调用示例

服务启动后执行。

```powershell
$baseUrl = 'http://127.0.0.1:8237'
$loginBody = @{ username = 'customer'; password = 'customer123' } | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($loginBody))
$headers = @{ Authorization = "Bearer $($login.token)" }

Invoke-RestMethod -Uri "$baseUrl/api/order" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/order/list" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/order/scope?feature=$([Uri]::EscapeDataString('批量导出'))" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/order/feature-scope?feature=$([Uri]::EscapeDataString('批量导出'))" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/order/renewal?window_days=30" -Headers $headers
```

管理接口需改用 `admin / admin123` 登录：

```powershell
$adminBody = @{ username = 'admin'; password = 'admin123' } | ConvertTo-Json -Compress
$adminLogin = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($adminBody))
$adminHeaders = @{ Authorization = "Bearer $($adminLogin.token)" }

Invoke-RestMethod -Uri "$baseUrl/api/order/admin?status=active&page=1&page_size=20" -Headers $adminHeaders
Invoke-RestMethod -Uri "$baseUrl/api/order/admin/O-1001" -Headers $adminHeaders
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/order/expire?limit=100" -Headers $adminHeaders
```

带中文查询参数时须做 URL 编码（`[Uri]::EscapeDataString`），否则响应为空。
