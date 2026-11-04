# 知识库、对话与健康检查 API

本文描述当前实现的接口。服务默认地址为 `http://127.0.0.1:8000`；在仓库根目录启动服务后，可访问：

- [Swagger UI](http://127.0.0.1:8000/docs)
- [ReDoc](http://127.0.0.1:8000/redoc)
- [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)

接口使用 JSON 请求与响应。OpenAPI 中有具体的响应模型和错误状态说明；`trace`、诊断 `stages` 与 `config` 保留可扩展的对象结构，字段含义见下文。工单与订单接口见 [tickets-api.md](tickets-api.md)。

## 登录与权限

管理员操作需要先调用 `POST /api/login`，然后发送 `Authorization: Bearer <token>`。演示管理员为 `admin / admin123`，客户为 `customer / customer123`。登录成功返回 `token` 和 `user`；凭据错误仍返回 HTTP 200，内容为 `{"token":"","user":{}}`。

| 接口 | 权限 | 成功响应 |
|---|---|---|
| `POST /api/chat` | 可匿名；有效登录态关联当前用户 | `ChatResponse` |
| `GET /api/conversation` | 可匿名；匿名或无效登录态返回空列表 | `ConversationResponse` |
| `GET /api/sources/{source_id}` | 无需登录 | `SourceDetail` |
| `POST /api/knowledge/ingest` | 管理员 | `IngestResponse` |
| `GET /api/knowledge/items` | 管理员 | `KnowledgeListResponse` |
| `GET /api/knowledge/ingest/pending` | 管理员 | `PendingIngestResponse` |
| `POST /api/knowledge/ingest/recover/{fingerprint}` | 管理员 | `IngestResponse` |
| `GET /api/knowledge/diagnose` | 管理员 | `KnowledgeDiagnosisResponse` |
| `GET /api/knowledge/settings` | 管理员 | `RetrievalSettings` |
| `PUT /api/knowledge/settings` | 管理员 | `RetrievalSettings` |
| `GET /health` | 无需登录 | `HealthResponse` |

对于结构合法的管理员请求，未登录、登录态无效或普通客户均返回 HTTP 403 和 `{"detail":"需要管理员权限"}`，当前不会返回 401。输入格式校验可能先于权限检查执行，非法输入可先返回 422。

## 提交文本资料

`POST /api/knowledge/ingest` 接收 JSON 文本，不接收文件或 multipart 上传。

| 字段 | 必填 | 约束与默认行为 |
|---|---|---|
| `title` | 是 | 字符串，1～200 字符；去除首尾空白后不得为空 |
| `content` | 是 | 字符串，1～100000 字符；去除首尾空白后不得为空 |
| `kind` | 否 | `manual`、`faq`、`rule`，默认 `manual`；会去空白并转小写 |
| `keywords` | 否 | 字符串数组；清理空项与去重后最多 20 项，默认空数组；留空时从标题提取 |

请求示例：

```json
{"title":"导出 FAQ","content":"导出失败时请检查权限，并使用 CSV 格式。","kind":"faq","keywords":["导出"]}
```

首次成功返回 HTTP 200，结构如下。编号由服务生成，下例仅展示形态：

```json
{"knowledge_id":"k-example","created":true,"chunks":1,"stage":"stored","detail":""}
```

幂等指纹由**规范化标题和正文**共同计算。同样的标题和正文重复提交，返回已有 `knowledge_id`、`created=false`、`chunks=0`、`stage="stored"`，`detail` 为“内容已存在，返回已有记录”。改变标题可能生成新条目；只改 `kind` 或 `keywords` 不会更新已存在的同指纹条目。

`chunks` 是本次计算出的正文分块数量，不是知识条目数。当前数据库保存整篇正文，尚未按块独立落库或检索。

通过当前新版入库服务共享同一 SQLite 文件时，同一指纹由 `knowledge_ingest_leases` 数据库租约协调执行：有效期 30 秒，每 10 秒心跳续租，时间由数据库 SQL 计算。最终所有权校验与知识写入在同一事务中执行，并再次查询已有资料；失去租约的旧执行者不能续租、落库或释放新执行者的租约。完成或异常时尝试释放，释放失败则等待到期。成功新建和重复提交仍使用原有 HTTP 200 响应。

相同资料正被占用或当前请求失去租约时，返回 HTTP 409，响应头为 `Retry-After: 1`，响应体例如：

```json
{"detail":"资料正在处理中，请稍后重试"}
```

失去租约时 `detail` 为“资料处理租约已失效，请稍后重试”。客户端应至少等待 1 秒后重试；此提示不保证 1 秒后租约已经释放或请求必定成功。

知识条目的指纹仍没有数据库唯一约束；上述并发保证只适用于经过新版入库服务且共享同一数据库文件的执行者，不覆盖绕过服务直接写库、不同数据库副本或历史重复记录。JSON 检查点仍尽力保存，不保证不同指纹并发写文件时完整，也不是完整任务 journal 恢复机制。

输入校验错误为 HTTP 422，`detail` 是错误数组，例如：

```json
{"detail":[{"type":"string_too_short","loc":["body","title"],"msg":"String should have at least 1 character","input":"","ctx":{"min_length":1}}]}
```

入库服务抛出的参数错误同样使用 422，但 `detail` 为字符串。客户端应同时处理两种形式，不依赖校验错误的英文文案。数据库与文件系统等异常并未统一转换为这一错误模型。

## 列表与来源回查

`GET /api/knowledge/items` 需要管理员权限，按资料 `id` 的字符串升序返回摘要。省略分页参数时返回全部摘要。响应保持以下结构。

```json
{"total":1,"items":[{"id":"k-example","title":"导出 FAQ","kind":"faq"}]}
```

| 查询参数 | 含义 |
|---|---|
| `offset` | 跳过的条目数，默认为 0，取非负整数 |
| `limit` | 最多返回的条目数，取 1 至 100。省略时返回偏移后的全部摘要 |

例如 `GET /api/knowledge/items?offset=1&limit=1` 返回第二条摘要。`total` 始终是资料总数，偏移等于或超过总数时 `items` 为空。参数越界或无法解析为整数时返回 HTTP 422，参数合法时非管理员返回 HTTP 403。分页的计数和窗口来自同一 SQLite 读事务。不同请求各自读取快照，翻页期间新增资料可能改变偏移位置。正文和关键词可通过来源回查读取。

`GET /api/sources/{source_id}` 使用列表中的 `id` 或回答中 `sources[].id` 回查原文。返回 `id`、`title`、`kind`、`content`、`keywords`；不存在时返回 HTTP 404 和 `{"detail":"来源不存在"}`。来源编号是持久化条目编号，不是 `c1` 这样的本轮引用句柄。当前来源详情不做登录或租户隔离检查。

## 对话与历史

`POST /api/chat` 的请求体为 `{"message":"导出失败怎么办？"}`。`message` 必须是字符串；当前未增加非空或长度上限校验。缺少字段或错误类型返回 422。

成功响应包含以下字段：

| 字段 | 含义 |
|---|---|
| `reply` | 回答文本，或安全的中文故障提示 |
| `suggest_ticket` | 是否建议继续转工单；不会因此自动建单 |
| `sources` | 本轮注册的来源数组，可为空；各项为 `id`、`handle`、`title`、`kind` |
| `trace` | 执行事件数组，一次性返回，不是 SSE 或 WebSocket 流 |

模型的引用会被转换成 `[来源: 标题]`。`sources` 是本轮检索并注册的来源，并不保证每一项都被回答引用。达到轮次上限前的最后一次检索可能尚未进入模型输入。`handle` 仅对本轮有效，回查原文使用 `id`。

模型调用失败仍返回 HTTP 200，通过 `reply` 和 `trace` 表达故障；来源与转人工建议仍按本轮已有信息生成。故障码和可重试标记见 [README 的对话故障提示](../README.md#对话故障提示)。`retryable` 表示适合稍后重试，不代表已经执行了重试。

事件都有 `kind` 和 `iteration`；首轮检索错误可能发生在 `iteration=0`，推理轮次从 1 开始。其余字段随类型变化：

| `kind` | 可能包含的字段 |
|---|---|
| `step` | `round` |
| `action` | `action`、`query` |
| `observe` | `count`、`query` |
| `done` | `length` |
| `stopped` | `reason` |
| `error` | `message`；服务异常带 `error_code`、`retryable`，空回答重试带 `retry`；检索失败可能带 `query` |

`GET /api/conversation` 返回当前用户最近会话的全部消息，按 `at`、消息 `id` 依次升序排列；时间戳相同以编号的字符串升序稳定决胜，响应仍不增加编号字段：

```json
{"messages":[{"role":"user","content":"导出失败怎么办？","at":1700000000.0},{"role":"assistant","content":"请补充导出格式。","at":1700000001.0}]}
```

`at` 是 Unix 秒级时间戳。没有会话、未登录或登录态无效时均返回 `{"messages":[]}`。当前没有分页或历史会话选择参数。

服务层模型历史窗口按 `at DESC, id DESC` 截取最近 N 条后反转（默认 8 条），与上述完整排序的相应后缀一致，再进行引用清理与预算压缩；窗口开头孤立的助手消息会跳过，保留后续用户消息开始的上下文。编号仅作稳定决胜，不代表真实发生顺序或旧消息配对，也不修复历史时间戳或提供跨请求快照保证。

“最近会话”在对话续写与本历史接口中使用同一规则：按会话的 `updated_at`、`created_at`、`id` 依次降序取第一条；更新时间相同取创建较晚的，两者相同取字符串编号较大的。编号仅作稳定决胜，不代表真实创建先后；旧会话与消息不会合并或改写。该规则不提供并发更新期间的跨请求快照一致性，也不改变会话内消息的排序规则。

首次 `POST /api/chat` 获取会话时，服务在查空后使用 SQLite 写锁复查并创建。共享同一 SQLite 文件并经新版服务调用时，同用户首次并发请求复用一个会话；已有会话仍选择最近一条，不合并历史多会话，也不提供整轮执行或消息排序保证。写锁为数据库级锁，仅覆盖首次创建，等待超时仍可能返回数据库故障 500，不自动重试，不新增 409 响应。绕过服务写库、不同数据库副本不受此保护；匿名用户仍共享 `anonymous`。

`POST /api/chat` 在同一数据库事务中保存本轮用户消息、助手消息及会话更新时间，事务内写入或提交前异常会回滚整轮，保留既有历史与更新时间。本轮助手的 `at` 严格大于用户的 `at`，但不保证跨轮或并发请求的消息顺序。此保证不涵盖新建空会话、已尝试步骤等独立事务；后续处理异常或客户端超时仍可能发生在消息提交之后，不能据此自动重发请求，接口也未增加重发去重能力。既有历史中的半轮或相同时间戳不会自动修复。

无效登录态在对话接口按匿名处理，不返回权限错误；匿名请求使用共享的 `anonymous` 用户标识，不提供独立访客会话保证。`POST /api/chat` 的执行摘要和转人工判定现按请求作用域隔离，`trace` 在本请求内读取并构造；成功或异常退出都会恢复外层上下文，避免并发串用摘要。请求/响应字段与错误状态未改变。此保证仅针对摘要与追踪，不保证同用户/匿名用户并发历史写入、消息顺序或模型客户端线程安全；本文 PowerShell 示例仍为串行调用。

服务层 `build_reply` 三元组和 `last_summary()` / `last_escalation()` 接口保持，但读取范围为当前执行上下文最近一次完整调用，不再是进程全局最新值。新调用开始时清空旧结果，未完成或未处理异常退出时不会发布旧摘要；HTTP 作用域结束后不能从其他上下文查询刚结束请求的摘要。

对话页在首次载入、浏览器重新联网或手动同步时调用 `/api/me` 校验身份，再读取 `/api/conversation`。历史读取失败最多尝试 3 次，不自动重发 `/api/chat`。客户端等待对话响应超过 60 秒会停止等待并标记结果不确定，服务端可能继续处理；稍后读取历史只能反映当时已保存的消息，不能确认尚未落库的请求已取消。历史响应只有文字、角色与时间，没有来源、执行过程或建单元数据，因此页面不重建这些控件。客户端恢复行为不改变本接口契约。

## 入库恢复

`GET /api/knowledge/ingest/pending` 返回 `{"items":[...]}`，按更新时间、指纹升序取最多 100 条未完成记录。每项包含 `fingerprint`、预留 `knowledge_id`、`stage`、`chunk_count`、`updated_at`（Unix 秒），不包含正文。记录可能仍在执行，列表不是空闲任务队列。

`POST /api/knowledge/ingest/recover/{fingerprint}` 无请求体，使用数据库 journal 内首次保存的输入同步恢复。`parsed` 会计算分块；`chunked` 复用计数；资料与 `stored` 完成状态同事务提交，完成后清空重复输入。重复恢复已完成项返回原资料、`created=false`、`chunks=0`。两接口仅管理员可用（403）；journal 不存在为 404；租约占用或失效为 409 并附 `Retry-After: 1`；输入指纹、格式或阶段不兼容为 422；底层数据库故障未统一分类，可能返回 500。

进程崩溃可能留下最长约 30 秒租约，过期后才能接管；服务不会启动自动恢复，也不自动轮询或重试。旧 JSON 检查点不能恢复原文，不导入新 journal。同指纹重提保持首次暂存的类型与关键词；恢复接口保留原预留编号，发现该编号已被其他资料占用时返回 422，需人工核对或普通重新提交。存储故障在首次输入提交前发生时需重新提供原文。恢复范围与数据保留限制见 [README](../README.md#入库-journal-恢复)。

使用登录示例取得 `$headers` 后可查看并恢复一项（会写入资料，正常完成后不再列在待恢复项中）：

```powershell
$baseUrl = 'http://127.0.0.1:8000'
$pending = Invoke-RestMethod -Uri "$baseUrl/api/knowledge/ingest/pending" -Headers $headers
if ($pending.items.Count -gt 0) {
    $fingerprint = $pending.items[0].fingerprint
    Invoke-RestMethod -Method Post -Uri "$baseUrl/api/knowledge/ingest/recover/$fingerprint" -Headers $headers
}
```

## 检索诊断

`GET /api/knowledge/diagnose?q=导出&top_k=3`：`q` 为必填字符串；`top_k` 为整数，默认 3，实际截断到 1～20，整数越界不会返回 422。缺少 `q` 或传入非整数 `top_k` 返回 422。

响应包含 `query`、实际 `top_k`、`prefetch_depth`、本次 `config`、各阶段 `stages` 和最终 `final_ids`。阶段名包括 `keyword_recall`、`vector_recall`、`rrf_fusion`、`rerank`、`mmr`；各阶段记录数量与条目编号，并可能包含 `skipped`、`note`、`detail`、`dropped`。

`config` 保留 `prefetch_depth_floor` 等既有字段，并包含 `max_rerank_candidates`、`composite_rerank`、`composite_recall`、`composite_source` 与 `source_weights`。重排阶段的 `detail.config` 为同次调用有效快照，与顶层一致；`detail.threshold` 是本次请求阈值，`effective_threshold` 在发生单次放宽时反映实际门槛。未装配 provider 时 `skipped=no_provider`，这些配置字段不代表已经执行重排或过滤。空候选也会直接跳过。服务层支持显式 `threshold` 覆盖，HTTP 未新增阈值查询参数，仍按部署配置读取；参数范围与环境变量见 [README](../README.md#检索设置与试查)。

服务层装配重排 provider 且正常/放宽阈值均无通过项时，`detail` 增加 `fallback_candidate_id`、`fallback_candidate_score`、`fallback_min_score`。候选按模型 `rerank_score` 最高选取，同分沿用复合排序；分数达到保底门槛则仅保留该项并置 `fallback=true`，否则返回空且 `fallback=false`。上述候选字段在拒绝保留时也存在，可据此解释空结果；`effective_threshold` 不会被改写为保底分。未进入保底检查时不附这些字段。HTTP 当前未装配重排 provider，通常不会走该分支；不因此启用模型或新增查询参数。

当前 HTTP 路由没有装配向量和重排 provider，诊断通常报告向量跳过、融合退回关键词结果、重排跳过；服务层具备对应能力不代表此接口已经发起模型调用。实际对话目前也使用关键词检索，诊断不是对某次对话的追踪回放。

向量阶段保留布尔值 `skipped`，新增 `detail` 原因对象。服务层可报告 `detail.skipped=no_provider/empty_corpus/model_error/invalid_response/no_positive_similarity`，分别表示未装配、空语料、调用失败、返回数量/维度/类型/有限性或非零性不合法、无正相似度结果；成功时 `detail` 可为空。向量返回不合法时整条分支弃用，关键词分支继续。重排阶段的 `detail.skipped=invalid_response` 表示返回形状、下标（含重复）或分数不合法，整批回退原候选，`model_error` 表示 provider 调用抛错，`empty` 表示合法空返回。有限越界分数仍截断到 0～1；合法部分返回不会被判为失败。原始 provider 异常不进入上述原因对象。完整校验范围见 [README](../README.md#检索设置与试查)。

## 检索设置

`GET /api/knowledge/settings` 返回六项当前生效设置。`PUT /api/knowledge/settings` 必须完整提交下列六项，成功时返回所保存的对象：

| 字段 | 类型 | 范围 | 默认值 |
|---|---|---|---|
| `prefetch_floor` | 整数 | 1～1000 | 50 |
| `prefetch_multiplier` | 整数 | 1～20 | 5 |
| `rrf_k` | 整数 | 1～1000 | 60 |
| `vector_weight` | 有限数值 | 0～1 | 0.7 |
| `keyword_weight` | 有限数值 | 0～1 | 0.3 |
| `mmr_lambda` | 有限数值 | 0～1 | 0.7 |

不接受数字字符串、布尔值、未知字段、非有限数值或缺少字段；两路权重不能同时为零，无需相加等于 1。非法输入返回 422，原设置不变。非管理员返回 403。文件写入失败返回 503 和 `{"detail":"检索设置保存失败，请检查配置目录写权限或稍后重试"}`，原子替换前的失败不会覆盖已有设置。

保存值优先于对应环境变量，写入本地 `backend/retrieval_settings.json`，后续调用重新读取、重启保留。最后成功写入者生效，无编辑冲突检测；不同配置文件的部署副本不自动同步。文件不存在时使用环境变量和默认值，文件损坏、不可读或校验失败时整体退回这两层并记录日志。环境两路权重同时为零时使用默认 0.7/0.3。候选深度为 `max(top_k * prefetch_multiplier, prefetch_floor)`；诊断响应的旧字段 `config.prefetch_depth_floor` 对应设置中的 `prefetch_floor`。

HTTP 诊断实际使用预取与 MMR 设置，MMR 仅在候选数多于目标条数时筛选；RRF 设置仅在服务层存在向量结果时使用。当前对话仍使用原有关键词检索，这些参数不会改变该路径或启用向量/重排模型。其他管理员可能同时更新设置，诊断响应中的 `config` 是本次实际采用的参数快照。

管理员登录取得 `$headers` 后可执行以下 PowerShell 示例（会保存全局检索设置，适用于共享本地配置文件的调用）：

```powershell
$baseUrl = 'http://127.0.0.1:8000'
$settings = Invoke-RestMethod -Uri "$baseUrl/api/knowledge/settings" -Headers $headers
$settings.mmr_lambda = 0.7
$settingsBody = $settings | ConvertTo-Json -Compress
Invoke-RestMethod -Method Put -Uri "$baseUrl/api/knowledge/settings" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($settingsBody))
Invoke-RestMethod -Uri "$baseUrl/api/knowledge/diagnose?q=$([Uri]::EscapeDataString('导出失败'))&top_k=3" -Headers $headers
```

## 健康报告

`GET /health` 返回 `status="ok"` 和 `models`。`models.all_available` 为配置检查的汇总值；`models.capabilities` 各项包含 `capability`、`available`、`hint`，能力名为 `chat`、`embedding`、`rerank`。

模型未配置时健康检查仍返回 HTTP 200。该报告不验证供应商在线、密钥认证、模型是否存在或协议是否被供应商支持，不能代替真实调用验收。

## PowerShell 调用示例

服务启动后执行。示例会写入一份知识资料，重复执行通常命中指纹；实际编号以返回值为准。需要已配置且可用的模型才能得到真实生成回答。

```powershell
$baseUrl = 'http://127.0.0.1:8000'
$loginBody = @{ username = 'admin'; password = 'admin123' } | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($loginBody))
if (-not $login.token) { throw '登录失败，请检查账号密码' }
$headers = @{ Authorization = "Bearer $($login.token)" }

$document = @{ title = '导出 FAQ'; content = '导出失败时请检查权限，并使用 CSV 格式。'; kind = 'faq'; keywords = @('导出') } | ConvertTo-Json -Compress
$result = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/knowledge/ingest" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($document))
Invoke-RestMethod -Uri "$baseUrl/api/knowledge/items" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/api/sources/$($result.knowledge_id)"
$query = [Uri]::EscapeDataString('导出')
Invoke-RestMethod -Uri "$baseUrl/api/knowledge/diagnose?q=$query&top_k=3" -Headers $headers

$message = @{ message = '导出失败怎么办？' } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/chat" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($message))
Invoke-RestMethod -Uri "$baseUrl/api/conversation" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/health"
```

契约测试使用临时数据库和模拟模型验证上述流程、权限与错误响应；不访问实际 `backend/app.db`，不验证真实模型供应商。运行方法见 [README](../README.md#运行测试)。
