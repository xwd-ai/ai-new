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

- **资料上传入库**：产品说明、FAQ、服务规则统一入库，作为回答的知识来源。
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

## 技术栈

- **后端**：Python + FastAPI + SQLAlchemy + SQLite
- **前端**：原生 HTML / CSS / JS
- **模型**：OpenAI 兼容接口（默认对接 DeepSeek，可替换）

## 快速开始

```bash
# 安装依赖
pip install -r backend/requirements.txt

# 配置模型密钥（复制模板后填写）
cp backend/.env.example backend/.env

# 启动（默认 127.0.0.1:8000）
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开 http://127.0.0.1:8000/ ，输入「专业版导出功能一直失败」即可体验完整链路。

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

## 运行测试

在仓库根目录执行，使用项目虚拟环境（以下为 Windows PowerShell 命令）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

开发依赖包含 `pytest` 和 `httpx`。测试使用临时 SQLite 数据库和模拟模型，覆盖工单流程、故障分类、缺失配置、检索降级与来源引用，不验证真实模型供应商的可用性。

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
```
