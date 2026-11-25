# AI 客服与工单助手

> **带证据、能追问、可交接**的售后支持系统 —— 让 AI 客服真正把问题解决到底，而不是只回一句话。

`Python 3.12+` · `FastAPI` · `Go 1.27` · `SQLAlchemy` · `SQLite` · `Alembic` · `3 万行代码` · `62 个测试文件`

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

**⑥ 按能力边界划分语言**  
生产默认拓扑是「Go 网关为唯一对外入口 + 三个 Go 服务」，Python 退为内部服务，只接受网关与内网调用。
纯 CPU 的检索打分、分块与后处理由 Go 检索服务承担；订单查询、令牌校验、待确认工单列表，以及工单的 `confirm / cancel / update_status` 三个状态流转由 Go 业务服务承担——后者只涉及规则校验、单表更新与时间线追加，不碰建单的取号与三层幂等锁；反向代理、入口鉴权、限流、SSE 转发、pprof 与优雅关闭由 Go 网关承担。
建单、批量操作与 LLM 编排留在 Python —— 建单依赖 `MAX(id)+1` 的应用层取号，跨语言写入必然撞号，要迁必须先把主键分配换成数据库侧保证；模型适配与多轮编排的生态也在 Python。
每个远程调用都保留一份本地同语义实现，作为**灰度、故障容错与独立回滚**路径：上游不可用时自动退回，便于按服务粒度排障。降级是可靠性设计，不是「少起几个服务」的默认用法。

## 架构

```
前端（原生 HTML / JS）
  ├─ 客户工作台 ── 对话、来源回查、执行过程
  └─ 管理后台 ──── 工单、知识库、检索设置
          │ HTTP
网关（Go :8080）—— 唯一对外入口，生产只暴露此端口
  ├─ 反向代理 → Python
  ├─ 入口鉴权   经业务服务验签，结果用于日志与限流维度
  ├─ 入口限流   token bucket，按用户 / IP 分桶
  ├─ /api/retrieval/stream   SSE 阶段进度转发
  └─ /debug/pprof            性能剖析
          │ HTTP
后端（FastAPI :8000，仅内网）—— LLM 编排、写事务与授权判定
  ├─ api/        路由与权限
  ├─ services/   业务：认证 / 订单 / 工单 / 知识 / 对话 / Agent
  ├─ models/     数据模型        schemas/  接口契约
  └─ providers/  模型适配（Chat / Embedding / Rerank）
          │                            │ HTTP（内部调用，失败自动降级）
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

生产部署只对外暴露网关的 8080：前端 → 网关 → Python 后端 → 两个 Go 服务构成一条链路，
Python 监听回环/内网地址，不直接接受公网请求。三个上游地址分别由 `BACKEND_URL`、
`GO_RETRIEVAL_URL` 与 `GO_BUSINESS_URL` 配置，可按服务粒度单独部署与回滚。
上游不可用时自动退回同语义的本地实现，用于灰度与故障容错 —— 降级是回滚路径，
不是「少起几个服务」的默认用法。

## 快速开始（生产默认拓扑）

Docker Compose 默认启动 MySQL 8、Python Agent 和三个 Go 服务。先在仓库根目录的
`.env` 配置相同供 Go 与 Python 使用的 `AI_NEW_JWT_SECRET` 和 `AGENT_INTERNAL_TOKEN`。
模型配置沿用 `backend/.env`。

```bash
docker compose up -d --build
```

浏览器访问 <http://127.0.0.1:8080>。Python 容器启动前执行 Alembic 升级。
设置 `GATEWAY_GO_PRIMARY=true` 可启用所有已迁移路由。

### 从已有 SQLite 导入

先停掉使用旧 SQLite 的应用写入。在空 MySQL 库上先升级、后导入，再启动应用，
以防演示种子数据占用目标表。下面使用 Compose 默认用户、密码和本地映射端口；
如设置了自定义 MySQL 配置，宿主机运行的命令使用同样的 `MYSQL_*` 环境变量
（宿主机端口用 `MYSQL_PORT`，对应 Compose 的 `MYSQL_PUBLISHED_PORT`）。

```bash
docker compose up -d --wait mysql
DB_DRIVER=mysql python -m alembic upgrade head
DB_DRIVER=mysql python -m scripts.import_sqlite_to_mysql backend/app.db
docker compose up -d --build
```

导入使用只读 SQLite 快照与单个 MySQL 写事务，目标业务表已有数据时拒绝执行。
既有 MySQL 里有数据时，用新的空 schema 进行导入。源 SQLite 可作为迁移前的备份。
Go 与 Python 必须连接同一 MySQL schema。

### 单独运行：Go 检索服务

compose 已包含该服务，下列命令用于不启容器时单独调试。

```bash
cd go-retrieval && go run .          # 默认 127.0.0.1:8081
```

`backend/.env` 里的 `GO_RETRIEVAL_URL=http://127.0.0.1:8081` 指定它的地址；
连接失败或超时时自动退回本地同语义实现，用于灰度与故障回滚。

### 单独运行：Go 业务服务

订单查询、待确认工单列表，以及工单的 `confirm / cancel / update_status` 状态流转。
compose 已包含该服务，下列命令用于不启容器时单独调试（需先启动过一次后端生成 `backend/app.db`）。

```bash
cd go-business && go run .          # 默认 127.0.0.1:8082，读 ../backend/app.db
```

`backend/.env` 里的 `GO_BUSINESS_URL=http://127.0.0.1:8082` 指定它的地址。
后端优先使用 Go 的查询与状态流转；连不上或响应无法识别时自动退回本地实现。
状态机规则两边一致——非法流转同样返回 `TICKET_INVALID_TRANSITION`，
未知状态同样按 422 语义拒绝，因此前端不需要知道工单在哪边处理。

本地可用 SQLite 单独调试。连接 MySQL 时设置 `DB_DRIVER=mysql` 并提供 `MYSQL_*` 连接参数。

### 网关：对外唯一入口

网关把 Python 后端与两个 Go 服务收在同一个入口后面：反向代理、入口鉴权、
限流、SSE 转发、pprof 与优雅关闭都在这一层完成。生产部署只暴露 8080，
Python 后端仅监听内网地址。

```bash
# 先启动内部 Python 后端（:8000），再起网关
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

### 本地单独运行 Python

直接运行 Python 时，默认使用 `backend/app.db`。设置 `DB_DRIVER=mysql` 会使用本机
MySQL，或显式设置 `DATABASE_URL` 指定 Python 的连接。Go 服务使用 `MYSQL_*`
连接参数，也可以通过 `MYSQL_DSN` 指定连接。

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
