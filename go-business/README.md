# 业务服务（Go）

把 Python 后端里**只读、无 LLM、无事务、无锁**的查询逐步迁移到 Go：
订单与服务范围判定、令牌校验、待确认工单列表。

## 为什么按这个顺序迁

| 模块 | 依赖 | 迁移范围 |
|---|---|---|
| 订单 / 服务范围 | 只读 `orders` / `products` | 查询与判定全量 |
| 令牌校验 | HMAC + 声明 | 仅校验层（登录与签发留 Python） |
| 待确认工单 | 只读 `tickets` | 仅列表查询（写状态机留 Python） |

共同点：验收标准明确（返回结构与 Python 逐字段一致）、失败可无损回滚
（去掉环境变量即恢复原路径）。

## 运行

```bash
go run .                                      # 默认 127.0.0.1:8082，读 ../backend/app.db
DB_PATH=/path/to/app.db ADDR=127.0.0.1:9000 go run .
```

数据库以**只读模式**打开（`mode=ro`）：当前阶段 Go 只承担查询，
从连接层面禁止写入，避免与 Python 争抢写权限。容器内同样以只读挂载使用，
见根目录 `docker-compose.yml`。

## 接口

| 方法 | 路径 | 对应 Python |
|---|---|---|
| GET | `/health` | — |
| GET | `/api/order?user_id=x` | `orders.query_order` |
| GET | `/api/orders?user_id=x` | `orders.query_orders` |
| GET | `/api/auth/verify` | `auth.resolve_token`（仅校验层） |
| GET | `/api/tickets/pending` | `tickets.list_pending_tickets` |

`/api/auth/verify` 从 `Authorization: Bearer` 头读令牌，不接受 query 参数——
query 会进入访问日志与浏览器历史，等于把凭据写进日志。

`/api/tickets/pending` 支持 `user_id`、`include_expired`、`timeout_seconds`
三个参数；响应字段与 Python 一致（`items` / `total` / `expired` /
`timeout_seconds` / `deadline`），每条工单另附 `expires_at` /
`remaining_seconds` / `expired`。负数 `timeout_seconds` 直接返回 400，
与 Python 的入参校验同口径。

### 关于工单迁移的边界

只迁移**只读列表**。建单、确认、取消、批量操作与状态流转仍留在 Python：
它们依赖「幂等键 + 数据库写锁 / 分布式锁 + 唯一约束」三层防护，
且属于低频写操作，重写风险远高于收益。Go 侧连接为 `mode=ro`，
即使写错 SQL 也无法改数据。

Python 侧优先调用本服务；未配置、调用失败、响应结构不完整，
或显式注入 `now` 的离线场景（远程用自己的时钟，无法对齐注入的时间口径），
都退回本地实现 —— 远程只是加速，不是正确性依赖。

### 关于认证迁移的边界

只迁移了**校验**，没有迁移登录与签发。原因：

1. 登录是低频写操作（要写会话表、bcrypt 哈希），迁移动机不足；
2. 校验虽然是每请求热点，但它是纯 HMAC 计算，Python 与 Go 都很快；
   **接进 Python 热路径反而每请求多一次网络往返，比本地计算更慢**。

因此 Go 的校验能力定位为「可独立部署的认证服务」（供网关或其他服务调用），
Python 主链路默认仍用本地实现。若确需统一到 Go，可在网关层前置，
而不是在业务请求里逐次转发。

跨语言互认已用 Python 签发的真实令牌验证：access 通过，refresh / 篡改签名 /
非法格式均被拒绝。

## 与 Python 的一致性保障

- `internal/orders/orders_test.go` 固化了 Python `evaluate_service_scope`
  在 9 种场景下的输出，Go 实现必须逐字段匹配；
- `internal/tickets/query_test.go` 覆盖默认过滤过期、包含过期、用户隔离、
  零阈值与负数阈值、倒序排列及字段形状，和 `Ticket.to_dict()` 对齐；
- `backend/tests/test_go_business_client.py` 从 Python 侧验证远程优先与降级路径。

```bash
go test ./... -race -v
```

任何字段漂移（包括百分比舍入、日期格式、空数组与 null 的区分）都会让测试失败。

## 已知差异

- **时间精度**：Python 用 `time.time()`，Go 用 `UnixNano()/1e9`，纳秒级差异；
  判定结果按天数与百分比取整，不影响输出。
- **查询时刻**：列表的 `deadline` / `remaining_seconds` 各自取本机时钟，
  跨进程比对可能差 1 秒；注入 `now` 的离线场景一律走 Python 本地实现。
- **舍入规则**：Python 的 `round(x, 1)` 是四舍六入五成双，Go 的 `math.Round`
  是五远离零 —— 这里实现了 `roundHalfEven` 专门对齐。

## 目录

```
main.go                    HTTP 入口与路由
Dockerfile                 多阶段构建
internal/db/db.go          SQLite 只读连接
internal/orders/
  types.go                 Order 与文本归一
  feature.go               版本能力矩阵与功能解析
  period.go                服务期判定（含舍入对齐）
  scope.go                 服务范围综合判定
  query.go                 取数与 query_order / query_orders
internal/auth/
  token.go                 JWT 校验（与 Python 密钥/声明互认）
internal/tickets/
  query.go                 待确认草稿列表与超时标记
```
