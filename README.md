# AI 客服与工单助手

> **带证据、能追问、可交接**的售后支持系统 —— 让 AI 客服真正把问题解决到底，而不是只回一句话。

`Python 3.12+` · `FastAPI` · `SQLAlchemy` · `SQLite` · `Alembic` · `3 万行代码` · `62 个测试文件`

---

## 解决什么问题

软件售后场景里，用户一句「专业版导出一直失败，解决不了就提交工单」，背后要做的事远不止给个回复：

- 先确认他买的是不是专业版、在不在服务期 —— **不然建议了也白搭**
- 从产品手册里找到对应的排查步骤，并且**说得清依据**
- 试了还是不行 → 判断什么时候该转人工
- 转人工前把「问题 + 已尝试步骤 + 排查依据」一起交接，**不让用户再讲第二遍**

这个项目把这套闭环完整做出来了。

## 核心能力

| 能力         | 说明                                |
| ---------- | --------------------------------- |
| **带来源问答**  | 回答标注引用来源，可点击回查原文，不凭空作答            |
| **多步排查**   | Agent 循环：思考 → 调用工具 → 观察结果 → 继续或结束 |
| **混合检索**   | 关键词 + 向量双路召回，RRF 融合，重排后 MMR 去冗余   |
| **订单范围判定** | 版本 + 服务期 + 功能，决定能否支持、要不要转人工       |
| **幂等建单**   | 同一问题并发提交，最终只产生一张工单                |
| **工单流转**   | 待确认 → 处理中 → 已解决 / 转人工，全程留痕        |
| **知识入库**   | 文本入库 + 租约心跳 + 断点恢复，失败可续跑          |
| **可观测**    | 执行过程事件流、检索诊断、离线评测报告               |

## 技术亮点

**① 回答必须带证据**  
模型只输出引用句柄，服务端解码为真实来源，前端可点开原文。检索不到支撑内容时，宁可少说也不硬编。

**② 幂等建单的三层防护**  
`幂等键哈希` + `数据库写锁` + `唯一约束` —— 并发提交同一问题，最终只落一张单。

**③ 检索链路的降级设计**  
BM25 与向量双路召回后 RRF 融合；重排全部不过阈值时，先放宽、再保底、最后**宁可返回空** —— 弱召回时不假装有答案。

**④ 入库的任务可靠性**  
数据库租约 + 心跳续租 + journal 检查点，进程崩溃后可从 `parsed → chunked → stored` 断点恢复。

**⑤ 工程完备度**  
62 个测试文件覆盖幂等、状态机、租约竞争、迁移、HTTP 全链路；Alembic 管理表结构演进；内置离线检索评测（Precision / Recall / MRR / nDCG）。

**⑥ 按能力边界逐步替换语言**  
纯 CPU 的检索打分、分块与检索后处理最先独立成 Go 检索服务；随后把订单查询、令牌校验、待确认工单列表这些「只读、无 LLM、无事务」的查询迁到 Go 业务服务；再把工单的 `confirm / cancel / update_status` 三个状态流转也迁过去——它们只涉及规则校验、单表更新与时间线追加，不碰建单的取号与三层幂等锁；最后加一层 Go 网关作为对外入口，承担反向代理、入口鉴权、限流、SSE 转发、pprof 与优雅关闭。
建单、批量操作与 LLM 编排仍留在 Python —— 建单依赖 `MAX(id)+1` 的应用层取号，跨语言写入必然撞号，要迁必须先把主键分配换成数据库侧保证；模型适配与多轮编排的生态也在 Python。
所有远程能力都可缺省：未配置或不可用时自动退回 Python 本地实现 —— **远程服务只是加速，不是正确性依赖**。

## 架构

```
前端（原生 HTML / JS）
  ├─ 客户工作台 ── 对话、来源回查、执行过程
  └─ 管理后台 ──── 工单、知识库、检索设置
          │ HTTP
网关（Go :8080，推荐作为对外入口，可缺省）
  ├─ 反向代理 → Python
  ├─ 入口鉴权   经业务服务验签，结果用于日志与限流维度
  ├─ 入口限流   token bucket，按用户 / IP 分桶
  ├─ /api/retrieval/stream   SSE 阶段进度转发
  └─ /debug/pprof            性能剖析
          │ HTTP
后端（FastAPI :8000）—— LLM 编排、写事务与授权判定
  ├─ api/        路由与权限
  ├─ services/   业务：认证 / 订单 / 工单 / 知识 / 对话 / Agent
  ├─ models/     数据模型        schemas/  接口契约
  └─ providers/  模型适配（Chat / Embedding / Rerank）
          │                            │ HTTP（可选，失败降级本地）
    SQLite / MySQL + Alembic    ┌─────┴──────┐
                                │            │
                     检索服务（Go :8081）  业务服务（Go :8082）
                       ├─ /search        BM25 + 向量 + RRF
                       ├─ /rank          复合分 + 阈值降级 + MMR
                       ├─ /ingest        分块 + 指纹（并发流水线）
                       ├─ /ingest/stats  流水线计数（排队 / 在途 / 失败）
                       └─ /stream        SSE 分阶段推送
                                             ├─ /api/order
                                             ├─ /api/orders
                                             ├─ /api/auth/verify
                                             ├─ /api/tickets/pending
                                             ├─ /api/tickets/confirm ┐
                                             ├─ /api/tickets/cancel  ├ 状态流转（写 SQLite）
                                             └─ /api/tickets/status  ┘
          │
    模型 API（OpenAI 兼容）
```

网关与两个 Go 服务相互独立，分别由 `BACKEND_URL`、`GO_RETRIEVAL_URL` 与
`GO_BUSINESS_URL` 控制，可以单独部署、单独回滚；任一不可用都只影响对应的加速路径。
不启动网关时直接访问 Python 的 8000 端口，行为与之前完全一致。

## 快速开始

要求已有可用的项目虚拟环境 `.venv`（本地验证使用 Python 3.12）。

```bash
# 1. 安装依赖
pip install -r backend/requirements.txt

# 2. 配置模型密钥（已有 backend/.env 时直接核对，避免复制模板覆盖原值）
cp backend/.env.example backend/.env

# 3. 启动（默认 127.0.0.1:8000）
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

打开 <http://127.0.0.1:8000> ，输入「**专业版导出功能一直失败**」，即可看到完整排查链路。

启动会初始化 SQLite 并预置演示数据，数据库路径固定为 `backend/app.db`；`--reload` 仅供本地开发。

> Windows 用户：把 `python` 换成 `.\.venv\Scripts\python.exe`

### 可选：启动 Go 检索服务

不启动也能正常使用 —— 后端检测不到该服务时会自动降级为本地实现，只是检索计算慢一些。

```bash
# 方式一：直接运行
cd go-retrieval && go run .          # 默认 127.0.0.1:8081

# 方式二：容器（连同 MySQL 一起起）
docker compose up -d go-retrieval
```

启动后在 `backend/.env` 里设置 `GO_RETRIEVAL_URL=http://127.0.0.1:8081`，后端即优先使用 Go 计算。

### 可选：启动 Go 业务服务

订单查询、待确认工单列表，以及工单的 `confirm / cancel / update_status` 状态流转。
与检索服务相互独立，同样不是启动硬依赖。

```bash
# 方式一：直接运行（默认读 ../backend/app.db）
cd go-business && go run .          # 默认 127.0.0.1:8082

# 方式二：容器（需先启动过一次后端，生成 backend/app.db）
docker compose up -d go-business
```

启动后在 `backend/.env` 里设置 `GO_BUSINESS_URL=http://127.0.0.1:8082`。
后端会优先使用 Go 的查询与状态流转；未配置、连不上、或返回无法识别时自动退回本地实现。
状态机规则两边一致——非法流转同样返回 `TICKET_INVALID_TRANSITION`，
未知状态同样按 422 语义拒绝，因此前端不需要知道工单在哪边处理。

写连接只支持 SQLite：Go 侧用 `mode=rw` 打开同一个库文件，与 Python 靠
`busy_timeout` 协调写锁。切换到 MySQL 部署时不要配置该项，让写路径留在 Python。

### 可选：启动 Go 网关（推荐作为对外入口）

网关把 Python 后端与两个 Go 服务收在同一个入口后面：反向代理、入口鉴权、
限流、SSE 转发、pprof 与优雅关闭都在这一层完成。**不启动也能正常使用**——
直接访问 Python 的 8000 端口，行为与之前完全一致。

```bash
# 先启动 Python 后端（:8000），再起网关
cd go-gateway && go run .          # 默认 127.0.0.1:8080
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BACKEND_URL` | `http://127.0.0.1:8000` | 必需，Python 后端地址 |
| `RETRIEVAL_URL` | 空 | 配置后启用 `/api/retrieval/stream` |
| `GO_BUSINESS_URL` | 空 | 配置后启用入口鉴权（经业务服务验签） |
| `GATEWAY_RATE_LIMIT` | `0` | 每秒令牌数，`0` 表示关闭入口限流 |
| `GATEWAY_RATE_BURST` | 同 rate | 桶容量 |
| `GATEWAY_PPROF` | `false` | 是否暴露 `/debug/pprof` |
| `GATEWAY_UPSTREAM_TIMEOUT` | `30s` | 等待上游响应头的超时（不影响 SSE 流式 body） |

启动后访问 <http://127.0.0.1:8080>，与直连 8000 等价；`/health` 会额外返回
`gateway: true`、各上游健康状态与能力开关。

网关**不接管授权判定**：它只做入口层令牌校验，结果用于日志与限流维度，
不写进转发给 Python 的请求头 —— 授权仍只有 Python 一处权威。
这样即使 Python 被直接暴露，也不存在可伪造的身份来源。

### 可选：切换到 MySQL

```bash
docker compose up -d mysql
# 在 backend/.env 里设置：
# DATABASE_URL=mysql+pymysql://ainew:ainew@127.0.0.1:3307/ainew?charset=utf8mb4
```

首次部署执行 `python -m alembic upgrade head` 建表；不设置则继续使用本地 SQLite。

## 演示账号

首次启动时由 `backend/seed.py` 自动预置演示账号（管理员 / 客户各一个），账号与初始口令见该脚本，此处不列出明文密码。

> 部署到公网前，请务必修改默认口令，并确认 `.env` 与数据库文件不进版本库。

## 文档

| 文档                                                                            | 内容              |
| ----------------------------------------------------------------------------- | --------------- |
| [接口文档](docs/api.md)                                                           | 字段、权限、错误响应、调用示例 |
| [认证](docs/auth-api.md) · [工单](docs/tickets-api.md) · [订单](docs/orders-api.md) | 各模块接口细节         |
| [检索评测](docs/retrieval-evaluation.md)                                          | 离线评测方法与消融结果     |
| [Go 网关](go-gateway/README.md) · [Go 检索](go-retrieval/README.md) · [Go 业务](go-business/README.md) | 三个 Go 服务的端点、配置与设计取舍 |
| [交付说明](docs/delivery-summary.md)                                              | 阶段成果与已知限制       |

## 测试

```bash
pip install -r backend/requirements-dev.txt
pytest backend/tests -q
```
