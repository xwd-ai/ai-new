# AI 客服与工单助手

面向软件公司的智能售后支持系统：用户在对话中提出问题，系统自动理解、追问、查询订单服务范围、检索产品手册给出带来源的建议，并在必要时创建工单转交人工跟进。

## 业务背景

在软件售后场景里，一个典型的问题往往是：「专业版导出功能一直失败，帮我看看，解决不了就提交工单。」

这类问题背后，实际需要系统完成一整套闭环，而不只是给一句回复：

1. **理解问题并追问** —— 澄清「哪个页面、报什么错、什么格式」，把模糊描述收敛成可处理的问题。
2. **查询订单与服务范围** —— 确认用户购买的是不是专业版、是否在服务期内、该功能是否在服务范围内。
3. **检索手册给出带来源建议** —— 从产品手册 / FAQ / 服务规则中检索，给出可回查出处的排查建议。
4. **决定继续排查或转人工** —— 根据用户反馈（「试了没用」）继续排查；命中转人工规则（超出服务范围、多次无效）则转人工。
5. **确认后创建工单** —— 向用户确认问题描述，幂等地创建工单，避免重复建单。
6. **沉淀处理记录** —— 工单内保存「问题描述 + 已尝试步骤 + 排查依据 + 处理状态」，供人工无缝接手。

因此，本产品是一个**「带证据、能追问、可交接」的售后助手**，而非单纯的聊天机器人。

## 核心能力

- **文本资料入库**：管理员通过 API 提交产品说明、FAQ、服务规则，作为回答的知识来源；当前不支持文件上传。
- **带来源问答**：检索知识库给出建议，回答标注来源，可点击回查。
- **订单查询**：查询用户的产品版本、服务期与功能范围，作为服务判定依据。
- **多步排查与追问**：对话式逐步排查，信息不足时主动追问。
- **确认后建单（防重复）**：建单前与用户确认，同一问题幂等去重，不重复创建。
- **处理记录与失败原因展示**：工单沉淀排查步骤、依据与状态，人工可跟进。

## 角色与页面

| 角色 | 页面 | 能力 |
|---|---|---|
| 客户 | 对话工作台 `/` | 与 AI 客服对话、查看带来源建议、确认创建工单 |
| 管理员 | 工单后台 `/admin` | 查看工单列表与详情、人工回复、流转处理状态 |
| 管理员 | 检索设置 `/settings` | 调整候选预取、RRF 和 MMR 参数，保存后运行检索试查 |

## 技术栈

- **后端**：Python + FastAPI + SQLAlchemy + SQLite
- **前端**：原生 HTML / CSS / JS
- **模型**：OpenAI 兼容接口（默认对接 DeepSeek，可替换）

## 快速开始

以下命令在仓库根目录的 Windows PowerShell 中执行，使用项目虚拟环境。

```powershell
# 安装依赖
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt

# 配置模型密钥（复制模板后填写）
Copy-Item backend/.env.example backend/.env

# 启动（默认 127.0.0.1:8000）
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开 http://127.0.0.1:8000/ ，输入「专业版导出功能一直失败」即可体验完整链路。

## API 契约与调用示例

[接口文档](docs/api.md) 说明知识入库、来源回查、对话、历史消息、检索诊断及健康报告的字段、权限、错误响应和 PowerShell 调用示例。服务启动后也可打开 [Swagger UI](http://127.0.0.1:8000/docs)、[ReDoc](http://127.0.0.1:8000/redoc) 或 [OpenAPI JSON](http://127.0.0.1:8000/openapi.json)。

入库按规范化标题与正文判重，当前保存整篇资料，`chunks` 仅是本次计算出的分块数。对话的 `trace` 为一次性返回的事件数组。对话目前使用关键词检索，HTTP 诊断路由也未装配向量或重排 provider，服务层具备这些能力不代表 HTTP 调用已经启用混合检索。

通过新版入库服务共享同一 SQLite 文件时，同指纹资料由数据库租约协调执行，租约有效期 30 秒、每 10 秒心跳续租；正常结束或异常时尝试释放，释放失败由到期机制兜底。占用中或失去租约返回 HTTP 409，并附 `Retry-After: 1`，建议至少等待 1 秒后重试，不保证届时已可处理。绕过服务直接写库、使用不同数据库副本或历史重复记录不在此保证范围内；JSON 检查点仍是尽力保存，不保证不同指纹并发写文件时完整，也不是任务 journal 恢复。

## 检索设置与试查

管理员登录后从工单后台进入「检索设置」，或打开 [设置页](http://127.0.0.1:8000/settings)。页面支持保存六项参数、撤销未保存修改，以及输入问题查看召回、融合、重排和 MMR 各阶段数量与最终来源 ID。修改后须先保存才能试查。读取与保存接口均要求管理员权限；参数类型、范围及错误响应见 [API 文档](docs/api.md#检索设置)。

设置保存在 `backend/retrieval_settings.json`，不进入 Git，也不修改 `.env`。优先级为：**管理页保存值 > 对应 `RETRIEVAL_*` 环境变量 > 默认值**。保存完整替换六项参数，通过同目录临时文件原子换名；最后成功写入者生效，不提供多管理员编辑冲突检测。后续调用重新读取配置，无需重启；共享同一配置文件的进程可读取相同设置，不同部署副本需分别配置。重启保留设置，部署时需保留该文件并保证目录可写。文件损坏、不可读或校验失败时整体退回环境变量与默认值，并记录 `retrieval.settings_read` 日志；环境变量中的两路权重同时为零时退回默认的 0.7/0.3。

当前 HTTP 试查实际使用候选预取和 MMR 参数；没有向量或重排 provider 时会标示跳过，RRF 参数仅影响服务层已提供向量结果的混合检索调用。**这些设置不会改变当前对话使用的关键词检索路径**，也不会启用模型。页面未开放尚未接入配置的重排参数。试查是独立调用，不代表某次对话的实际来源；其他管理员可能同时保存，本次结果以服务读取值为准。

## 数据库迁移

`backend/requirements.txt` 已包含 Alembic。迁移默认复用应用的数据库引擎，操作 `backend/app.db`，无需在 `alembic.ini` 中另配连接串。已有数据库升级前，先停止服务并备份数据库，再执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
# 查看迁移目标与数据库当前版本
.\.venv\Scripts\python.exe -m alembic heads
.\.venv\Scripts\python.exe -m alembic current
# 升级并检查结构是否与应用模型一致
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m alembic current
.\.venv\Scripts\python.exe -m alembic check
```

当前目标版本为 `c94e1a7f5208`，在 `e5b7a91c2046` 冻结的 10 张表基础上新增 `knowledge_ingest_leases`，共 11 张表；`d1178ec7c657` 保留为历史接入点。空库、由应用 `create_all` 建立的旧库，以及已登记历史版本的库，都通过 `upgrade head` 接入，也兼容 `create_all` 已预建的租约表。迁移先检查已有表的列、类型、可空性、主键及重复唯一键，再创建缺失表或补齐约束；不兼容结构或重复键会中止升级，保留已有数据，不自动删除或合并记录。

旧知识表缺少的 `fingerprint` 列及索引会补齐，补列不回填旧资料的指纹；已存在的指纹和正文保持原值。缺失的用户名、工单去重键唯一约束也会补齐。重复升级不会重复创建结构。不要通过 `stamp` 绕过检查。

需要重建的表若含自定义触发器、CHECK 或外键约束，会在结构变更前拒绝自动升级；必需索引存在同名但定义冲突时也会拒绝。此类旧库需先人工核对。

应用启动仍保留 `create_all` 和原有补列逻辑，不会自动运行 Alembic，升级需显式执行上述命令。基线及租约迁移不支持自动降级，也不支持 `--sql` 离线模式；需要回退时使用升级前的数据库备份恢复。

## 对话故障提示

`GET /health` 中的 `models` 报告模型配置是否齐全、是否仍为占位值；它不调用供应商接口，因此不代表模型在线或密钥已通过认证。

`POST /api/chat` 的模型调用失败时，仍返回 HTTP 200 和原有的 `reply`、`sources`、`suggest_ticket`、`trace` 字段。`reply` 提供中文故障提示；服务异常对应的 `trace` 错误事件增加 `error_code` 和 `retryable`，例如：

```json
{"kind": "error", "iteration": 1, "message": "模型调用失败：服务响应超时，请稍后重试。", "error_code": "timeout", "retryable": true}
```

| `error_code` | 含义 | `retryable` |
|---|---|---|
| `timeout` | 调用超时 | `true` |
| `connection_error` | 无法连接服务 | `true` |
| `rate_limited` | 模型服务限流 | `true` |
| `model_configuration` | 缺失或占位密钥、模型服务认证或权限失败 | `false` |
| `upstream_unavailable` | 模型服务返回 5xx | `true` |
| `model_request_rejected` | 模型服务拒绝请求 | `false` |
| `internal_error` | 未识别的执行异常 | `false` |

`retryable` 表示是否适合稍后重试，不表示本轮已经执行了重试。配置和请求错误应由管理员排查；原始异常文本不会进入这些故障提示、执行事件或检索失败观察。

空密钥或已识别的占位密钥不会阻止应用导入；首次生成回答时会提示联系管理员，且不会创建聊天模型客户端。首轮或后续检索失败时记录错误事件，允许模型继续回答，此时可能没有来源。上述分类只覆盖对话中的模型调用、知识检索和工具执行；数据库读写、知识库管理 API 和启动异常未统一纳入。

## 日志配置

应用日志输出到标准输出，支持以下环境变量；修改后重启服务生效：

| 变量 | 默认值 | 行为 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 控制应用日志级别，非法值退回 `INFO` |
| `LOG_FORMAT` | 纯文本 | 设置为 `json` 时输出单行 JSON，其余值使用纯文本 |

例如启用 JSON 日志：

```powershell
$env:LOG_LEVEL = "INFO"
$env:LOG_FORMAT = "json"
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

当前埋点包括关键词检索 `retrieval.keyword` 的命中情况，对话每轮 `agent.step` 的毫秒耗时、输出长度或模型错误分类，以及入库心跳或租约释放失败的告警。日志覆盖范围限于这些调用点，尚未覆盖所有 HTTP 请求或全部检索阶段。

## 运行测试

在仓库根目录执行，使用项目虚拟环境（以下为 Windows PowerShell 命令）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

开发依赖包含 `pytest` 和 `httpx`。测试使用临时 SQLite 数据库和模拟模型，覆盖工单流程、故障分类、缺失配置、检索降级与来源引用，不验证真实模型供应商的可用性。

迁移测试通过 `Config.attributes["connection"]` 注入临时连接，覆盖空库建表、旧库及已登记历史版本的库接入、资料保留、指纹列和索引补齐、唯一约束修复、重复升级，以及不兼容结构、重复键和基线降级的拒绝行为，不操作实际 `backend/app.db`。

API 契约测试核对 OpenAPI 响应模型、权限及错误状态，并串行验证登录、入库去重、列表、来源详情、诊断、对话与历史消息链路；真实模型回答和并发隔离尚未由这些测试验证。

租约测试使用临时 SQLite 文件，覆盖线程及独立进程竞争、心跳续租、过期接管、失去租约后禁止落库、异常释放及 HTTP 409 重试提示；迁移测试同时覆盖租约表创建与 `create_all` 预建表接入。这些测试不访问实际 `backend/app.db`。

## 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| admin | admin123 | 管理员 |
| customer | customer123 | 客户 |

## 目录结构

```
backend/
  main.py          # 应用入口与路由装配
  database.py      # 数据库连接（SQLAlchemy + SQLite）
  seed.py          # 预置演示数据
  providers.py     # 模型适配层（Chat / Embedding / Rerank）
  models/          # 数据模型（用户 / 订单 / 工单 / 知识 / 会话）
  schemas/         # 接口契约（Pydantic）
  services/        # 业务逻辑（认证 / 订单 / 工单 / 知识 / 对话）
  api/             # HTTP 路由
frontend/
  login.html       # 登录页
  index.html       # 客户对话工作台
  admin.html       # 管理员工单后台
  settings.html    # 管理员检索设置与试查
  retrieval-settings.js # 设置页交互
```
