# 认证 API

本文描述认证（模块 1）相关接口的请求、响应与错误码。工单与订单接口见 [tickets-api.md](tickets-api.md)，知识库、对话与健康检查接口见 [api.md](api.md)。

服务默认地址为 `http://127.0.0.1:8000`，所有接口前缀为 `/api`，请求与响应均为 JSON。也可在 [Swagger UI](http://127.0.0.1:8000/docs) 中查看自动生成的 OpenAPI 结构。

## 接口清单

| 接口 | 权限 | 说明 |
|---|---|---|
| `POST /api/login` | 无需登录 | 登录，签发访问令牌与续期令牌 |
| `POST /api/register` | 无需登录 | 注册客户账号 |
| `POST /api/refresh` | 凭续期令牌 | 用续期令牌换发访问令牌 |
| `POST /api/logout` | 凭访问令牌 | 登出并作废当前令牌 |
| `POST /api/sessions/cleanup` | 管理员 | 清理过期登录凭证 |
| `GET /api/me` | 可匿名 | 返回当前登录用户 |

演示账号为 `admin / admin123`（管理员）与 `customer / customer123`（客户）。

## 错误响应

认证模块的业务失败统一返回以下结构，HTTP 状态码与 `error_code` 同时给出：

```json
{"error":"密码强度不足","error_code":"AUTH_WEAK_PASSWORD","error_detail":{"violations":["需同时包含字母与数字"]}}
```

- `error`：可直接展示的中文文案，措辞可能调整。
- `error_code`：机器可读标识，调用方应以此分支处理。
- `error_detail`：可选，只在有补充上下文时出现，例如未通过的密码规则列表、缺失的声明名。

请求体结构校验失败（字段缺失、类型错误、超出长度）由框架先行拦截，返回的是另一种结构，`detail` 为错误数组：

```json
{"detail":[{"type":"missing","loc":["body","password"],"msg":"Field required","input":{"username":"docuser03"}}]}
```

客户端需同时处理这两种 422。业务校验发生在框架校验之后，因此同一接口可能返回两种 422 形态。

**登录是唯一例外**：凭据错误不走错误结构，而是返回 HTTP 200 与 `{"token":"","user":{}}`，前端据此区分「凭证错误」与「接口异常」（见下文登录一节）。

错误码与 HTTP 状态对照：

| error_code | HTTP | 触发场景 |
|---|---|---|
| `AUTH_INVALID_USERNAME` | 422 | 用户名格式不符合规则 |
| `AUTH_USERNAME_TAKEN` | 409 | 用户名已被占用（比较忽略大小写） |
| `AUTH_WEAK_PASSWORD` | 422 | 密码强度不足，`error_detail.violations` 逐条列出未通过规则 |
| `AUTH_TOKEN_MISSING` | 401 | 请求未携带令牌 |
| `AUTH_TOKEN_MALFORMED` | 401 | 令牌结构不合法：段数不对、含非法字符、超长、头部或载荷不是 JSON 对象 |
| `AUTH_TOKEN_UNSUPPORTED_ALG` | 401 | 签名算法不受支持（含 `alg=none` 这类降级尝试） |
| `AUTH_TOKEN_BAD_SIGNATURE` | 401 | 签名与载荷不匹配：令牌被篡改或由其它密钥签发 |
| `AUTH_TOKEN_EXPIRED` | 401 | 令牌已过期 |
| `AUTH_TOKEN_NOT_YET_VALID` | 401 | 令牌尚未生效：`nbf` 或 `iat` 晚于当前时间并超出时钟偏差 |
| `AUTH_TOKEN_BAD_CLAIMS` | 401 | 声明缺失或取值不符：签发方、使用方、类型（如拿访问令牌换发）、时间戳，或续期令牌不属于当前登录用户 |
| `AUTH_REFRESH_UNKNOWN` | 401 | 续期令牌不在登记表内，或与登记信息不一致 |
| `AUTH_REFRESH_REVOKED` | 401 | 续期令牌已被吊销（登出或异常处理时失效） |
| `AUTH_REFRESH_USER_GONE` | 401 | 续期令牌指向的账号已不存在 |
| `AUTH_FORBIDDEN` | 403 | 已登录但角色不具备该操作权限 |
| `AUTH_TOKEN_SIGNING_FAILED` | 500 | 令牌签发失败：用户信息缺少签发所需字段 |

令牌校验失败（`AUTH_TOKEN_*`）与续期失败（`AUTH_REFRESH_*`）都返回 401，客户端拿到任一都应引导重新登录；区别只在于「哪一环不作数」，便于前端提示与日志定位。

## 登录

`POST /api/login`

| 字段 | 必填 | 约束 |
|---|---|---|
| `username` | 是 | 字符串 |
| `password` | 是 | 字符串 |

成功响应：

```json
{
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
  "user": {"id":"u-customer","username":"customer","name":"李女士","role":"customer"},
  "token_type": "Bearer",
  "expires_in": 1800,
  "expires_at": 1794148401,
  "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
  "refresh_expires_in": 2592000,
  "refresh_expires_at": 1796738601
}
```

| 字段 | 说明 |
|---|---|
| `token` | 访问令牌，后续请求放 `Authorization: Bearer <token>` |
| `user` | 公开用户信息：`id`、`username`、`name`、`role` |
| `token_type` | 固定 `Bearer` |
| `expires_in` / `expires_at` | 访问令牌剩余有效秒数 / 到期时间戳（秒） |
| `refresh_token` | 长期续期令牌，用于换发（见「换发」） |
| `refresh_expires_in` / `refresh_expires_at` | 续期令牌剩余有效秒数 / 到期时间戳（秒） |

凭据错误（账号不存在、密码错误、入参为空）**不返回错误结构**，而是 HTTP 200 与：

```json
{"token":"","user":{}}
```

失败分支不返回任何令牌与有效期字段（响应按「不输出空字段」序列化），响应体与旧契约完全一致。字段缺失或类型错误仍由框架返回 422 错误数组。

### 令牌结构

两类令牌都是三段式紧凑 JWT（`header.payload.signature`），签名算法固定 HS256。载荷声明：

| 声明 | 含义 |
|---|---|
| `iss` / `aud` | 签发方 `ai-new` / 使用方 `ai-new-web` |
| `sub` | 用户 id，回查用户的唯一依据 |
| `username` / `role` | 供日志与展示参考，权限判定一律回库确认 |
| `typ` | 令牌用途：`access` 或 `refresh`，两类令牌不能互相顶替 |
| `iat` / `nbf` / `exp` | 签发、生效、过期时间戳（秒），`nbf` 与 `iat` 取同一时刻 |
| `jti` | 一次性标识，登出作废名单按它登记 |

访问令牌默认 30 分钟有效，续期令牌默认 30 天，均可通过环境变量调整（见「配置」）。校验按「结构 → 算法 → 签名 → 声明 → 时效」逐级进行，签名未验证的载荷不会被当作可信数据读取。

## 注册

`POST /api/register`，注册客户账号；身份固定为 `customer`，管理员账号不经由此接口创建。

| 字段 | 必填 | 约束 |
|---|---|---|
| `username` | 是 | 3–32 位，字母开头，其余可为字母、数字、下划线、点、连字符；去首尾空白后校验 |
| `password` | 是 | 明文密码，仅在本次请求内用于哈希；不做去空白处理 |
| `name` | 否 | 显示名称，≤32 字符，超出截断；留空时取用户名 |

密码强度规则（任一不满足即 422，`violations` 逐条列出）：

- 长度至少 8 位，且不超过 72 字节（超出部分不参与哈希计算，直接拒绝而非静默截断）；
- 不能包含空格等空白字符；
- 需同时包含字母与数字；
- 不能是同一个字符的重复；
- 不能使用常见弱口令（如 `12345678`、`password`、`admin123`）；
- 用户名（≥3 字符）不能出现在密码里，比较忽略大小写。

成功响应：

```json
{"user":{"id":"u-b65299935a","username":"docuser01","name":"文档用户","role":"customer"},"message":"注册成功"}
```

用户名占用返回 409，比较忽略大小写（`Admin` 与 `admin` 视为同名）：

```json
{"error":"用户名已被占用","error_code":"AUTH_USERNAME_TAKEN"}
```

密码强度不足返回 422：

```json
{"error":"密码强度不足","error_code":"AUTH_WEAK_PASSWORD","error_detail":{"violations":["不能包含用户名"]}}
```

用户名格式不符返回 422：

```json
{"error":"用户名需 3–32 位，以字母开头，仅可含字母、数字、下划线、点或连字符","error_code":"AUTH_INVALID_USERNAME"}
```

字段缺失、超长等结构问题由框架返回 422 错误数组。密码经 bcrypt 哈希后落库，明文不进入返回值与日志；旧版摘要的存量账号在下一次登录成功时自动升级，无需重置密码。

## 换发

`POST /api/refresh`，用长期续期令牌换一张新的访问令牌。续期令牌先取请求体，请求体没给再取 `Authorization` 头；两处都空时按未携带令牌拒绝（401 `AUTH_TOKEN_MISSING`）。

```json
{"refresh_token":"eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…"}
```

`refresh_token` ≤4096 字符。响应与登录成功时的令牌字段同名同义，客户端可直接复用解析逻辑：

```json
{
  "token": "eyJhbGciOiJIUzI1NiIs…",
  "user": {"id":"u-customer","username":"customer","name":"李女士","role":"customer"},
  "token_type": "Bearer",
  "expires_in": 1800,
  "expires_at": 1794148456,
  "refresh_token": "eyJhbGciOiJIUzI1NiIs…",
  "refresh_expires_in": 2591880,
  "refresh_expires_at": 1796738656
}
```

语义要点：

- 换发只认 `typ=refresh` 且登记表中未吊销、未过期的凭证；拿访问令牌来换发会被拒绝（401 `AUTH_TOKEN_BAD_CLAIMS`）。校验顺序为「签名与声明 → 作废名单 → 登记记录 → 账号是否仍在」。
- 续期凭证**原样回传、不轮换**：`refresh_token` 与提交的相同，其有效期仍以首次登录为准，`refresh_expires_in` 只会随时间递减，换发不重置。
- 新访问令牌同样登记为会话记录，对话链路按令牌查会话的路径不受影响。
- 换发失败一律 401（`AUTH_REFRESH_UNKNOWN` / `AUTH_REFRESH_REVOKED` / `AUTH_TOKEN_EXPIRED` / `AUTH_REFRESH_USER_GONE` 等），意味着客户端手里的凭证已彻底不能用，应直接回登录页。

## 登出

`POST /api/logout`，作废当前访问令牌，可选一并吊销续期令牌。访问令牌从 `Authorization` 头取，缺失即 401 `AUTH_TOKEN_MISSING`——没有令牌可作废时返回成功只会让客户端误以为本地状态已清干净。请求体可选：

```json
{"refresh_token":"eyJhbGciOiJIUzI1NiIs…"}
```

响应：

```json
{
  "message": "已退出登录",
  "user": {"id":"u-customer","username":"customer","name":"李女士","role":"customer"},
  "revoked_access": true,
  "access_expires_at": 1794148456,
  "session_removed": true,
  "revoked_refresh": true,
  "refresh_expires_at": 1796738656
}
```

| 字段 | 说明 |
|---|---|
| `user` | 登出前确认的身份，账号已删则为空对象 |
| `revoked_access` | 访问令牌本次是否新登记为作废 |
| `access_expires_at` | 作废的访问令牌原到期时间戳（秒） |
| `session_removed` | 是否同时删除了对应的会话记录 |
| `revoked_refresh` | 续期令牌本次是否新登记为作废 |
| `refresh_expires_at` | 被吊销的续期令牌原到期时间戳（秒），未提交则为 `null` |

语义要点：

- **已过期的访问令牌仍可登出**：客户端换发失败后需要一条退出的路。时效判定放宽，但签名、算法与声明的校验一步不减，伪造令牌进不了作废名单。
- 续期令牌可选；给了就必须属于当前登录用户，否则整体拒绝（401 `AUTH_TOKEN_BAD_CLAIMS`），不留下「访问令牌已作废、续期令牌却没吊销」的半截结果。
- **重复登出幂等**：返回同样的成功语义，`revoked_access` / `revoked_refresh` 只说明本次是否新增作废记录，第二次起为 `false`，前端不必为重复退出写分支。
- 登出后同一张访问令牌再来访问一律按未登录处理（`/api/me` 返回 `{"user":null}`），直到它自然过期。作废记录保留至令牌到期，可由清理接口删除。

## 当前用户

`GET /api/me`，无请求体。身份取自令牌声明并经回库确认，账号被删除后旧令牌即失效，不会因为令牌仍在有效期内而返回一个已不存在的用户。

```json
{"user":{"id":"u-customer","username":"customer","name":"李女士","role":"customer"}}
```

令牌缺失、校验失败或已登出时返回 `{"user":null}`，**不返回 401**——本接口用于探测登录态，未登录是正常答案。调用方需要强制登录时应使用业务接口自身的 401。

## 过期凭据清理

`POST /api/sessions/cleanup`（仅管理员，未登录 401、非管理员 403）。删除三张表中已过期的记录：`sessions`（过期会话）、`refresh_tokens`（过期续期登记）、`revoked_tokens`（已过拦截期的作废记录）。全部参数可选，空请求体按默认口径执行：

| 字段 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `retention` | 0 | 0–30 天（秒） | 过期后额外保留的秒数；排查「刚过期就登不上」时可留观察窗口 |
| `limit` | 2000 | 1–10000 | 单表单次删除上限，积压较多时分多次调用 |

```json
{
  "message": "已清理 9 条过期登录记录",
  "deleted": {"sessions":9,"refresh_tokens":0,"revoked_tokens":0},
  "total": 9,
  "cutoff": 1794146712,
  "retention": 0,
  "truncated": false
}
```

| 字段 | 说明 |
|---|---|
| `deleted` | 各表删除条数 |
| `total` | 本次删除总条数 |
| `cutoff` | 过期判定的截止时间戳（秒）：早于它的记录都被视作失效 |
| `retention` | 本次生效的保留窗口（秒） |
| `truncated` | 是否因达到单次上限而可能仍有剩余，为 `true` 时再次调用即可继续清理 |

## 身份与权限约定

请求携带 `Authorization: Bearer <token>`；只给令牌原文也接受，便于手工联调。身份解析顺序：令牌结构 → 签名算法 → 签名 → 声明 → 时效 → 作废名单 → 按 `sub` 回库取用户。任何一环不通过都视为未登录；回库确认意味着**改名、调角色下一次请求即生效**，账号删除后旧令牌立刻失效。

角色只有两种，权限分三档，由路由的依赖声明决定：

| 档位 | 依赖 | 行为 |
|---|---|---|
| 可匿名 | `current_user` | 未登录返回 `None`，接口按匿名处理（如 `/api/me`、`/api/chat`） |
| 要求登录 | `require_login` | 未登录 401 |
| 要求管理员 | `require_admin` | 未登录 401，非管理员 403 |

401 与 403 分开报：401 提示客户端去登录，403 说明已登录但不该做这件事。工单、知识库等模块的路由共用这套依赖，错误体结构与本模块一致。

旧版单段会话令牌（无分隔点的随机串）仍按会话表解析，仅供未换发新令牌的客户端过渡使用。

## 配置

签名密钥与有效期取自环境变量，调用时解析，改配置后重启进程即生效：

| 环境变量 | 默认值 | 允许区间 | 说明 |
|---|---|---|---|
| `AI_NEW_JWT_SECRET` | 内置开发密钥 | 建议 ≥32 字节 | 签名密钥；未配置时使用开发密钥并告警，**生产环境必须覆盖** |
| `AI_NEW_JWT_TTL` | 1800（30 分钟） | 60–604800（7 天） | 访问令牌有效期（秒） |
| `AI_NEW_JWT_REFRESH_TTL` | 2592000（30 天） | 3600–15552000（180 天） | 续期令牌有效期（秒） |
| `AI_NEW_JWT_LEEWAY` | 30 | 0–300 | 时钟偏差余量（秒） |

取值必须是纯数字（写成 `30m`、`1h` 按非法值处理）；超出区间按默认值处理并告警，不会因为一处配置写错就让服务起不来。

## PowerShell 调用示例

服务启动后执行。示例会注册一个临时账号并完成登录、换发、登出全流程。

```powershell
$baseUrl = 'http://127.0.0.1:8000'

$registerBody = @{ username = 'docuser01'; password = 'Doc2026pass'; name = '文档用户' } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/register" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($registerBody))

$loginBody = @{ username = 'docuser01'; password = 'Doc2026pass' } | ConvertTo-Json -Compress
$login = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($loginBody))
if (-not $login.token) { throw '登录失败，请检查账号密码' }
$headers = @{ Authorization = "Bearer $($login.token)" }

Invoke-RestMethod -Uri "$baseUrl/api/me" -Headers $headers

$refreshBody = @{ refresh_token = $login.refresh_token } | ConvertTo-Json -Compress
$refreshed = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/refresh" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($refreshBody))

$logoutBody = @{ refresh_token = $login.refresh_token } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/logout" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($logoutBody))
```

管理员登录后可继续调用过期凭据清理：

```powershell
$adminBody = @{ username = 'admin'; password = 'admin123' } | ConvertTo-Json -Compress
$admin = Invoke-RestMethod -Method Post -Uri "$baseUrl/api/login" -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($adminBody))
$adminHeaders = @{ Authorization = "Bearer $($admin.token)" }
Invoke-RestMethod -Method Post -Uri "$baseUrl/api/sessions/cleanup" -Headers $adminHeaders -ContentType 'application/json; charset=utf-8' -Body '{}'
```
