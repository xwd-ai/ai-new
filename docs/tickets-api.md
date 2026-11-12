# 工单与订单 API

本文描述工单（模块 3）与订单（模块 2）相关接口的请求、响应与错误码。知识库、对话与健康检查接口见 [api.md](api.md)，登录、注册、换发、登出等认证接口见 [auth-api.md](auth-api.md)。

服务默认地址为 `http://127.0.0.1:8000`，所有接口前缀为 `/api`，请求与响应均为 JSON。也可在 [Swagger UI](http://127.0.0.1:8000/docs) 中查看自动生成的 OpenAPI 结构。

## 登录与权限

工单接口均需登录：先调用 `POST /api/login`，再发送 `Authorization: Bearer <token>`。演示账号为 `admin / admin123`（管理员）与 `customer / customer123`（客户）。

权限分三档：

| 档位 | 说明 |
|---|---|
| 无需登录 | 无；订单接口未登录时返回空订单，其余接口一律拒绝 |
| 本人 / 管理员 | 详情、确认、取消、转人工判定：普通用户仅限本人工单，管理员不限 |
| 仅管理员 | 回复、改状态、转人工、批量关闭、批量转人工、超时收敛 |

未登录或 token 无效返回 HTTP 401，普通用户调用管理接口返回 HTTP 403，两者响应体结构与业务错误一致（见下节）。

## 工单状态与流转

状态取值：

| 状态 | 中文名 | 含义 |
|---|---|---|
| `pending_confirm` | 待确认 | 建单后的草稿状态，等待用户确认，不直接进入待处理 |
| `open` | 待处理 | 草稿确认后的正式工单 |
| `in_progress` | 处理中 | 已有客服回复或已开始跟进 |
| `transferred_human` | 已转人工 | 超出服务范围或排查无效，转交人工 |
| `resolved` | 已解决 | 问题闭环 |
| `closed` | 已关闭 | 取消、超时收敛或人工关闭后的终态 |

合法流转表如下，未列出的流转一律拒绝：

| 当前状态 | 可流转到 |
|---|---|
| `pending_confirm` | `open`、`closed` |
| `open` | `in_progress`、`resolved`、`transferred_human`、`closed` |
| `in_progress` | `resolved`、`transferred_human`、`closed` |
| `transferred_human` | `in_progress`、`resolved`、`closed` |
| `resolved` | `closed` |
| `closed` | 无（终态） |

所有状态变更都会在同一事务内写入 `ticket_status_logs` 时间线记录（原状态、新状态、操作人、变更时间），建单首条记录的 `from_status` 为空字符串。

## 错误响应

工单模块的业务失败统一返回以下结构，HTTP 状态码与 `error_code` 同时给出：

```json
{"error":"工单不存在","error_code":"TICKET_NOT_FOUND","error_detail":{"ticket_id":"T-9999"}}
```

- `error`：可直接展示的中文文案，措辞可能调整。
- `error_code`：机器可读标识，调用方应以此分支处理。
- `error_detail`：可选，只在有补充上下文时出现，例如非法流转的原/目标状态、未命中规则时的判定结论。

请求体结构校验失败（字段缺失、类型错误、超出长度）由框架先行拦截，返回的是另一种结构，`detail` 为错误数组：

```json
{"detail":[{"type":"missing","loc":["body","description"],"msg":"Field required","input":{}}]}
```

客户端需同时处理这两种 422。业务校验发生在框架校验之后，因此同一接口可能返回两种 422 形态。

错误码与 HTTP 状态对照：

| error_code | HTTP | 触发场景 |
|---|---|---|
| `TICKET_UNAUTHORIZED` | 401 | 未登录、token 无效或已过期 |
| `TICKET_FORBIDDEN` | 403 | 普通用户调用管理接口，或访问他人工单 |
| `TICKET_NOT_FOUND` | 404 | 工单号不存在 |
| `TICKET_INVALID_TRANSITION` | 409 | 流转不在合法流转表内 |
| `TICKET_UNKNOWN_STATUS` | 422 | 请求里出现未知状态值 |
| `TICKET_INVALID_PAYLOAD` | 422 | 通用入参非法 |
| `TICKET_INVALID_DESCRIPTION` | 422 | 问题描述为空（服务层兜底，HTTP 通常先由请求校验拦截） |
| `TICKET_INVALID_STATUS_FILTER` | 422 | 列表筛选里出现未知状态 |
| `TICKET_INVALID_PAGINATION` | 422 | 页码或每页条数越界 |
| `TICKET_INVALID_ORDER` | 422 | 排序方向不是 `asc` / `desc` |
| `TICKET_INVALID_KEYWORD` | 422 | 关键词超过 50 字符 |
| `TICKET_INVALID_BATCH` | 422 | 批量工单号为空或超过上限 |
| `TICKET_INVALID_STATS_RANGE` | 422 | 统计天数不在 1–365 之间 |
| `TICKET_TRANSFER_RULE_NOT_MATCHED` | 409 | `auto=true` 但未命中转人工规则 |

## 接口清单

| 接口 | 权限 | 说明 |
|---|---|---|
| `POST /api/tickets` | 本人 | 建单，落地为待确认草稿 |
| `GET /api/tickets` | 本人 / 管理员 | 列表，支持状态、关键词、分页、排序 |
| `GET /api/tickets/{ticket_id}` | 本人 / 管理员 | 详情，含三个结构化分区 |
| `GET /api/tickets/pending` | 本人 / 管理员 | 待确认草稿列表 |
| `POST /api/tickets/confirm` | 本人 / 管理员 | 确认草稿，`pending_confirm` → `open` |
| `POST /api/tickets/cancel` | 本人 / 管理员 | 取消草稿，`pending_confirm` → `closed` |
| `POST /api/tickets/expire` | 管理员 | 超时草稿批量收敛 |
| `POST /api/tickets/reply` | 管理员 | 追加人工回复 |
| `POST /api/tickets/status` | 管理员 | 手工改状态 |
| `POST /api/tickets/transfer` | 管理员 | 转人工 |
| `GET /api/tickets/{ticket_id}/transfer-decision` | 本人 / 管理员 | 转人工判定（只读） |
| `POST /api/tickets/batch-close` | 管理员 | 批量关闭 |
| `POST /api/tickets/batch-transfer` | 管理员 | 批量转人工 |
| `GET /api/tickets/stats` | 本人 / 管理员 | 状态计数与处理时长统计 |
| `GET /api/order` | 未登录返回空 | 当前用户订单与服务范围 |

## 建单

`POST /api/tickets`

| 字段 | 必填 | 约束 | 说明 |
|---|---|---|---|
| `description` | 是 | 1–500 字符，去空白后不得为空 | 问题描述，也是幂等键的组成部分 |
| `tried_steps` | 否 | ≤1000 字符 | 已尝试步骤，可写多步（换行、分号、`·`、`1.` 编号均可切分） |
| `evidence` | 否 | ≤1000 字符 | 排查依据，可写「结论（来源：产品手册第 3 节）」形式的来源标注 |

```json
{"description":"专业版批量导出失败","tried_steps":"1. 重启客户端 2. 换网络","evidence":"导出任务超时（来源：产品手册第 3 节）"}
```

`tried_steps` 留空时，服务会取当前用户在对话中已记录的排查步骤自动填入。

响应：

```json
{
  "ticket": {
    "id": "T-1014",
    "dedup_key": "5c115eaf…",
    "user_id": "u-customer",
    "user_name": "李女士",
    "description": "专业版批量导出失败",
    "tried_steps": "1. 重启客户端 2. 换网络",
    "evidence": "导出任务超时（来源：产品手册第 3 节）",
    "status": "pending_confirm",
    "created_at": 1792998107.4,
    "replies": []
  },
  "created": true,
  "message": "工单已生成待确认草稿，请确认后创建"
}
```

建单只创建草稿，`status` 恒为 `pending_confirm`；只有确认接口才会流转为 `open`。

幂等语义：同一用户与同一问题描述（去空白后完全一致）已有未解决工单时，直接返回原工单，`created=false`，`message` 为「该问题已有未解决工单，已返回原工单」。已解决（`resolved`）的工单不参与命中去重，同问题可再次建单。并发建单在幂等键上加写锁串行执行，后到者命中已有工单，不会重复创建。

描述为空或纯空白时由请求校验拦截，返回 422 及错误数组，`msg` 为「问题描述不能为空」；字段缺失或超长同样返回 422 错误数组。服务层保留同一校验，错误码为 `TICKET_INVALID_DESCRIPTION`，正常走 HTTP 时不会出现。

## 确认与取消

`POST /api/tickets/confirm`，请求体 `{"ticket_id":"T-1014"}`，成功返回 `{"ticket": {…}}`，状态由 `pending_confirm` 变为 `open`。

`POST /api/tickets/cancel`，请求体 `{"ticket_id":"T-1014","reason":"问题已自行解决"}`，`reason` 可选（≤500 字符），成功返回 `{"ticket": {…}}`，状态变为 `closed`；取消原因以 `system` 来源写入回复列表，同时落一条状态时间线。

两个接口都是幂等的：工单已不在 `pending_confirm` 时（例如已被确认、已关闭、已被并发确认），不报错也不重复流转，原样返回当前工单。工单不存在返回 404，越权返回 403。

## 待确认列表

`GET /api/tickets/pending`

| 参数 | 默认 | 说明 |
|---|---|---|
| `include_expired` | `false` | 为 `true` 时连同已超时但尚未收敛的草稿一起返回 |
| `timeout_seconds` | 1800 | 覆盖默认超时阈值，用于联调 |
| `all_users` | `false` | 管理员需要全量视图时置 `true`；普通用户恒为本人的工单 |

```json
{
  "tickets": [
    {"id":"T-1014","status":"pending_confirm","expires_at":1792999907.4,"remaining_seconds":1721,"expired":false,"…":"工单基础字段"}
  ],
  "total": 1,
  "expired": 0,
  "timeout_seconds": 1800,
  "message": "共 1 个待确认工单"
}
```

每项在工单基础字段外附加 `expires_at`（确认截止时间戳）、`remaining_seconds`（剩余可确认秒数，超时后为 0）与 `expired`，前端据此展示倒计时。列表按建单时间倒序。

## 超时收敛

`POST /api/tickets/expire?timeout_seconds=1800&limit=200`（仅管理员，两个参数均可省略）

把「建单时间早于当前时间减阈值」且仍为 `pending_confirm` 的草稿批量关闭，每个工单写入取消说明与时间线。整个扫描在固定写锁内串行执行，多个调度同时触发时先到者收敛完毕，后到者按同条件重查已无候选，重复调用不会重复关闭。

| 字段 | 说明 |
|---|---|
| `timeout_seconds` | 本次生效的超时阈值，默认 1800 秒 |
| `deadline` | 截止时间戳，早于该时间建单的草稿视为超时 |
| `scanned` | 本次扫描到的候选数 |
| `closed` | 实际关闭数 |
| `closed_ids` | 被关闭的工单号列表 |
| `remaining` | 仍待收敛的超时草稿数，大于 0 时应继续下一批 |

## 列表

`GET /api/tickets`

| 参数 | 默认 | 说明 |
|---|---|---|
| `status` | 不筛选 | 单状态、逗号分隔多状态、重复传参三种写法均支持；`all` 或不传表示不筛选 |
| `keyword` | 无 | ≤50 字符，模糊匹配工单号、问题描述、客户名，任一命中即保留 |
| `q` | 无 | `keyword` 的别名，`keyword` 优先 |
| `page` | 1 | 页码，从 1 开始 |
| `page_size` | 20 | 每页条数，1–100，越界拒绝而非静默截断 |
| `order` | `desc` | 按建单时间排序，`asc` 正序 / `desc` 逆序 |

状态值会去空白并转小写，重复项去重，未知状态返回 `TICKET_INVALID_STATUS_FILTER`。关键词按字面量匹配，其中的 `%` 与 `_` 不作通配符。

```json
{
  "tickets": [{"id":"T-1013","status":"closed","…":"工单基础字段"}],
  "total": 4,
  "page": 1,
  "page_size": 5,
  "pages": 1,
  "has_more": false,
  "order": "desc",
  "statuses": ["open","closed"],
  "keyword": "导出",
  "message": "按 状态 open/closed + 关键词「导出」 筛选到 4 个工单"
}
```

`total` 是符合筛选条件的总条数，不受分页限制。管理员看到全部工单，普通用户只看到本人工单，隔离在查询层完成。时间戳相同的记录以工单号兜底排序，翻页不重不漏。

## 详情

`GET /api/tickets/{ticket_id}`，返回 `{"ticket": {…}, "message": "工单 T-1013，留存 1 条已尝试步骤…"}`。

`ticket` 在基础字段（`id`、`dedup_key`、`user_id`、`user_name`、`description`、`tried_steps`、`evidence`、`status`、`created_at`、`replies`）之外，追加四个结构块。原始文本字段照旧返回，便于兼容既有展示。

`troubleshooting`：把原始排查文本切成结构化条目。

| 字段 | 说明 |
|---|---|
| `tried_steps` | 条目数组，每项为 `{"index":1,"text":"重启客户端"}` |
| `evidence` | 条目数组，每项为 `{"index":1,"text":"导出任务超时","source":"产品手册第 3 节"}`；无来源标注时 `source` 为空串 |
| `tried_step_count` / `evidence_count` | 条目数 |
| `has_tried_steps` / `has_evidence` | 是否有内容 |

切分支持换行、中英文分号、`·`/`•`、短横线列表与 `1.`/`（1）`/`①` 编号；重复条目只保留首次出现；单条超过 200 字符截断并补省略号；每类最多 50 条。

`status_timeline`：状态轨迹数组，按变更时间升序，每项含 `id`、`ticket_id`、`from_status`、`to_status`、`operator`、`changed_at`（Unix 秒）。建单首条的 `from_status` 为空串。

`reply_timeline`：回复轨迹与统计。

| 字段 | 说明 |
|---|---|
| `items` | 逐条回复：`index`、`replier`、`content`、`at`、`at_text`、`source`（`human`/`system`）、`is_human` |
| `items[].kind` / `reason` / `reason_codes` / `operator` | 仅转人工说明附带，用于还原判定依据 |
| `total` / `human_count` / `system_count` | 回复总数与人工/系统分布 |
| `has_replies` / `has_human_reply` | 是否有回复 / 是否有人工回复 |
| `first_reply_at` / `last_reply_at` / `last_human_reply_at` | 首末回复与最后人工回复时间 |
| `first_response_seconds` | 建单到首条人工回复的秒数，无人工回复时为 `null` |
| `transfer_count` / `last_transfer_reason` | 转人工次数与最近一次原因 |
| `truncated` | 回复超过 100 条时为 `true`，只保留最近 100 条并重排序号 |

`detail_summary`：一屏概览，含 `tried_step_count`、`evidence_count`、`status_change_count`、`status_chain`（按序的目标状态数组）、`last_changed_at`、`reply_count`、`human_reply_count`、`system_reply_count`、`has_human_reply`、`first_response_seconds`、`transfer_count`。

工单不存在返回 404，普通用户访问他人工单返回 403（`error_detail` 里含动作、工单号与所属人）。

## 回复

`POST /api/tickets/reply`（仅管理员）

| 字段 | 必填 | 约束 |
|---|---|---|
| `ticket_id` | 是 | 1–64 字符 |
| `content` | 是 | 1–2000 字符，去空白后不得为空 |

`open` 状态的工单会随回复自动流转到 `in_progress`，`in_progress` 的工单只追加回复不产生流转；其他状态（`pending_confirm`、`closed`、`resolved`、`transferred_human`）由流转校验拒绝并返回 409。响应为 `{"ticket": {…}}`，含追加后的 `replies` 与当前 `status`。

## 改状态

`POST /api/tickets/status`（仅管理员），请求体 `{"ticket_id":"T-1014","status":"resolved"}`。

按合法流转表校验：不在表内返回 409 与 `TICKET_INVALID_TRANSITION`，`error_detail` 给出 `from_status` 与 `to_status`；状态值未知返回 422 与 `TICKET_UNKNOWN_STATUS`。校验在改写任何字段之前完成，被拒绝时工单保持原样。成功返回 `{"ticket": {…}}`。

`ticket_id` 缺失时按工单不存在处理，返回 404。

## 转人工

`POST /api/tickets/transfer`（仅管理员）

| 字段 | 必填 | 约束 | 说明 |
|---|---|---|---|
| `ticket_id` | 是 | 1–64 字符 | 工单号 |
| `reason` | 否 | ≤500 字符 | 转人工原因；为空时用规则判定结果回填，都为空时用「客服手动转人工」 |
| `user_text` | 否 | ≤500 字符 | 用户当前消息，用于识别主动要求转人工 |
| `feature` | 否 | ≤50 字符 | 问题涉及的功能，用于判断是否在当前版本能力内 |
| `auto` | 否 | 默认 `false` | 为 `true` 时只在规则判定建议转人工时才执行，否则返回 409 |

响应在 `{"ticket": {...}, "decision": {...}, "message": "..."}` 中，`ticket.transfer` 单独给出本次转人工的 `from_status`、`to_status`、`reason`、`reason_codes`、`operator`、`at`、`at_text` 与 `by_rule`（是否由规则命中触发）。

`auto=true` 未命中规则时返回 409 与 `TICKET_TRANSFER_RULE_NOT_MATCHED`，`error_detail.decision` 内含完整判定结论，可据此向用户解释为何不转。非法来源状态（如已关闭）由流转校验拒绝。

服务范围取自**工单所属用户**的订单，而非当前登录账号，管理员处理他人工单时结论仍对应工单本人。

### 转人工判定

`GET /api/tickets/{ticket_id}/transfer-decision?user_text=…&feature=…`（本人 / 管理员，只读）

按优先级返回命中原因，不改变工单状态也不追加回复：

| 顺序 | `reason_codes` | 命中条件 |
|---|---|---|
| 1 | `user_requested` | 用户消息或工单描述中出现转人工诉求（转人工、人工客服、找客服、升级处理等） |
| 2 | `out_of_scope` | 无订单、服务到期或订单非有效状态、所问功能不在当前版本能力内 |
| 3 | `ineffective_retries` | 已尝试步骤 ≥3 步，或人工回复 ≥2 条仍未闭环 |

```json
{
  "decision": {
    "ticket_id": "T-1014",
    "status": "in_progress",
    "applicable": true,
    "should_transfer": true,
    "suggested_status": "transferred_human",
    "reason_codes": ["user_requested"],
    "reasons": [{"code":"user_requested","label":"用户主动要求转人工","detail":"命中「转人工」"}],
    "reason_text": "用户主动要求转人工（命中「转人工」）",
    "basis": {
      "tried_step_count": 3, "tried_step_threshold": 3,
      "human_reply_count": 0, "human_reply_threshold": 2,
      "feature": "", "has_order": true, "in_service": true, "matched_keyword": "转人工"
    },
    "message": "建议转人工：用户主动要求转人工（命中「转人工」）"
  },
  "message": "建议转人工：…"
}
```

`applicable` 表示当前状态能否流转到 `transferred_human`，`pending_confirm`、`closed`、`resolved`、`transferred_human` 均为 `false`，此时 `should_transfer` 恒为 `false`，`message` 说明「无需做转人工判定」。`suggested_status` 仅在建议转人工时非空。

## 批量操作

`POST /api/tickets/batch-close` 与 `POST /api/tickets/batch-transfer`（仅管理员），请求体：

```json
{"ticket_ids":["T-1013","T-1014"],"reason":"长期未响应，运营关闭"}
```

`ticket_ids` 为 1–50 项，空白项被丢弃，重复工单号只处理一次；全部无效返回 422 与 `TICKET_INVALID_BATCH`，超过 50 项由框架返回 422 错误数组。`reason` 可选（≤500 字符），写入每张工单的回复时间线。

```json
{
  "result": {
    "action": "close",
    "requested": 2,
    "succeeded": 1,
    "failed": 1,
    "success_ids": ["T-1014"],
    "failures": [{"ticket_id":"T-9999","error_code":"TICKET_NOT_FOUND","reason":"工单不存在","from_status":""}],
    "all_succeeded": false
  },
  "message": "批量关闭完成：成功 1 个，失败 1 个（T-9999：工单不存在）"
}
```

逐单独立结算：个别工单不存在或当前状态不允许流转，只记入 `failures`（含错误码、原因与流转前状态），同批其他工单照常提交，调用方据 `success_ids` 与 `failures` 决定是否重试失败部分。

## 统计

`GET /api/tickets/stats`

| 参数 | 默认 | 说明 |
|---|---|---|
| `days` | 不限制 | 只统计最近 N 天创建的工单，1–365，越界或非整数返回 `TICKET_INVALID_STATS_RANGE` |
| `user_id` | 无 | 按客户过滤，仅管理员可用；普通用户传他人账号返回 403 |

普通用户只统计本人工单；管理员不传 `user_id` 统计全量，传了则只看该客户。

```json
{
  "stats": {
    "total": 8,
    "status_counts": {"open":7,"in_progress":0,"resolved":0,"pending_confirm":0,"transferred_human":0,"closed":1},
    "status_breakdown": [{"status":"open","label":"待处理","count":7,"percent":87.5}],
    "active": {"count":7,"statuses":["open","in_progress","pending_confirm","transferred_human"],"percent":87.5},
    "settled": {"count":1,"statuses":["resolved","closed"],"percent":12.5,"completion_rate":12.5},
    "handling_duration": {
      "sample_size": 1, "total_seconds": 6.0, "avg_seconds": 6.0,
      "min_seconds": 6.0, "max_seconds": 6.0, "median_seconds": 6.0, "avg_text": "6 秒",
      "by_status": {"resolved": {"sample_size":0,"avg_seconds":null,"avg_text":"—"},"closed": {"sample_size":1,"avg_seconds":6.0,"avg_text":"6 秒"}}
    },
    "range": {"days": null, "since": null, "until": 1792998200.0},
    "user_id": "u-customer",
    "generated_at": 1792998200.0
  },
  "message": "共 8 个工单：待处理 7 个、已关闭 1 个；平均处理时长 6 秒（样本 1 个，闭环率 12.5%）"
}
```

`status_counts` 固定包含全部六个状态，计数为 0 也出现，前端不必补零。处理时长按「建单 → 时间线最后一次进入终态（`resolved` / `closed`）」计算，只统计已结案工单；样本为 0 时各指标为 `null`、`avg_text` 为 `—`，不返回 0 秒以免误读。

## 订单

`GET /api/order` 返回当前登录用户的订单与服务范围，供工单页与转人工判定使用。未登录或 token 无效时返回 `{"order": null}`，不返回 401。

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

无订单时只返回 `{"has_order": false, "message": "未查询到该用户的订单记录"}`。`in_service` 同时要求订单状态为 `active` 且未过期。当前一个用户只返回一条订单记录。

同一判定逻辑还以只读工具 `query_order` 的形式供对话链路调用，注册方式、三种查询形态与结果字段见 [订单工具化说明](order-tool.md)。

## PowerShell 调用示例

服务启动后执行。示例会创建并关闭一张临时工单，实际工单号以返回值为准。

```powershell
$baseUrl = 'http://127.0.0.1:8237'
$loginBody = @{ username = 'customer'; password = 'customer123' } | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($loginBody))
$headers = @{ Authorization = "Bearer $($login.token)" }

$body = @{
    description = '专业版批量导出失败'
    tried_steps = "1. 重启客户端`n2. 换网络"
    evidence    = '导出任务超时（来源：产品手册第 3 节）'
} | ConvertTo-Json -Compress
$created = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/tickets" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$ticketId = $created.ticket.id

Invoke-RestMethod -Method Post -Uri "$baseUrl/api/tickets/confirm" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes((@{ ticket_id = $ticketId } | ConvertTo-Json -Compress)))
Invoke-RestMethod -Uri "$baseUrl/api/tickets/$ticketId" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/tickets?status=open,closed&page=1&page_size=5" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/tickets/stats" -Headers $headers
$decision = Invoke-RestMethod -Uri "$baseUrl/api/tickets/$ticketId/transfer-decision?user_text=$([Uri]::EscapeDataString('帮我转人工'))" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/order" -Headers $headers
```

管理员接口需改用 `admin / admin123` 登录，可继续调用回复、改状态、转人工、批量关闭与超时收敛。
