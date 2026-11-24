# 业务服务（Go）

把 Python 后端里**边界清晰、不依赖 LLM 编排**的部分逐步迁移到 Go：
订单与服务范围判定、令牌校验、待确认工单列表，以及工单的三个状态流转。

## 为什么按这个顺序迁

| 模块 | 依赖 | 迁移范围 |
|---|---|---|
| 订单 / 服务范围 | 只读 `orders` / `products` | 查询与判定全量 |
| 令牌校验 | HMAC + 声明 | 仅校验层（登录与签发留 Python） |
| 待确认工单 | 只读 `tickets` | 仅列表查询 |
| 工单状态流转 | 单表更新 + 时间线追加 | `confirm` / `cancel` / `update_status` |

共同点：验收标准明确（返回结构与 Python 逐字段一致）、失败可无损回滚
（去掉环境变量即恢复原路径）。

**建单没有迁**，原因不是「Go 写不了」，而是工单号由 `MAX(id)+1` 在应用层分配。
跨语言写入时两边各自取最大值必然撞号，要迁必须先把主键分配换成数据库侧保证
（序列表或自增列）——那是在改共享契约，不该混在「状态流转」这次迁移里。

## 运行

```bash
go run .                                      # 默认 127.0.0.1:8082，读 ../backend/app.db
DB_PATH=/path/to/app.db ADDR=127.0.0.1:9000 go run .
```

数据库**分两条连接**，按最小权限隔离读写能力：

| 入口 | 模式 | 用途 | 关键参数 |
|---|---|---|---|
| `db.Open` | `mode=ro` | 订单、工单列表、令牌校验 | 物理只读，写错 SQL 也改不了数据 |
| `db.OpenWrite` | `mode=rw` | 工单状态流转 | `busy_timeout(5000)`、`_txlock=immediate`、连接池 1 |

两个参数的理由：Python 与 Go 可能同时写同一个 SQLite 文件，没有
`busy_timeout` 就会随机报 `database is locked`；`_txlock=immediate` 让事务
一开始就拿写锁，避开 SQLite 经典的「读事务升级写锁」失败。连接池限制为 1，
因为 SQLite 是单写者模型，多连接并发写只会互相等锁。

容器内该文件以可写方式挂载，见根目录 `docker-compose.yml`。

## 接口

| 方法 | 路径 | 对应 Python |
|---|---|---|
| GET | `/health` | — |
| GET | `/api/order?user_id=x` | `orders.query_order` |
| GET | `/api/orders?user_id=x` | `orders.query_orders` |
| GET | `/api/auth/verify` | `auth.resolve_token`（仅校验层） |
| GET | `/api/tickets/pending` | `tickets.list_pending_tickets` |
| POST | `/api/tickets/confirm` | `tickets.confirm_ticket` |
| POST | `/api/tickets/cancel` | `tickets.cancel_ticket` |
| POST | `/api/tickets/status` | `tickets.update_status` |

`/api/auth/verify` 从 `Authorization: Bearer` 头读令牌，不接受 query 参数——
query 会进入访问日志与浏览器历史，等于把凭据写进日志。

`/api/tickets/pending` 支持 `user_id`、`include_expired`、`timeout_seconds`
三个参数；响应字段与 Python 一致（`items` / `total` / `expired` /
`timeout_seconds` / `deadline`），每条工单另附 `expires_at` /
`remaining_seconds` / `expired`。负数 `timeout_seconds` 直接返回 400，
与 Python 的入参校验同口径。

### 关于工单迁移的边界

已迁：待确认列表（读）、`confirm` / `cancel` / `update_status`（写）。
未迁：建单、批量操作、超时收敛、转人工判定——建单卡在取号方式上（见上文），
批量操作要跨多张单汇总部分成功结果，超时收敛是定时任务，都属于下一层的事。

三个写接口的语义与 Python 严格对齐：

| 动作 | 幂等性 | 流转校验 |
|---|---|---|
| `confirm` | 已非 `pending_confirm` 时原样返回，不报错 | 仅在草稿状态才走状态机 |
| `cancel` | 同上，且不重复追加取消说明 | 同上 |
| `update_status` | **不幂等**：同状态会被拒绝 | 每次都要过流转表 |

`update_status` 不做幂等短路是刻意的：它是管理端的显式动作，
传入「已经是该状态」通常说明调用方状态没刷新，报错比静默成功更有用。

错误映射与 Python 一一对应，Python 侧据此还原成同一套异常类型：

| 情况 | HTTP | error_code |
|---|---|---|
| 工单不存在 | 404 | `TICKET_NOT_FOUND` |
| 状态值不认识 | 422 | `TICKET_UNKNOWN_STATUS` |
| 状态值合法但这条流转不允许 | 409 | `TICKET_INVALID_TRANSITION` |

降级策略与读接口**不同**：4xx 是业务结论，Python 直接采用、不再走本地；
只有连不上、超时、响应不是 JSON、错误码认不出时才退回本地实现——
否则「非法流转」会在本地再执行一遍，白白多一个写入窗口。

Python 侧的只读查询仍是老规矩：未配置、调用失败、响应结构不完整，
或显式注入 `now` 的离线场景，都退回本地实现 —— 远程只是加速，不是正确性依赖。

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
- `internal/tickets/transition_test.go` 正向遍历流转表每一条边，同时枚举
  6×6 全部状态组合，保证「表里没有的组合一律拒绝」；并单独锁住
  「同状态不算合法流转」；
- `internal/tickets/write_test.go` 覆盖幂等、并发确认只产生一条时间线、
  非法流转不改动任何字段、`replies` 追加与时间线写入；
- `backend/tests/test_go_business_client.py` 与
  `backend/tests/test_go_ticket_write_client.py` 从 Python 侧验证
  远程优先、404/409/422 映射与降级路径。

```bash
go test ./... -race -v
```

除单测外还做过一次影子比对：同一份临时 SQLite 上，Python 与 Go 各处理一张
等价工单，逐项核对返回字段、落库状态、`replies` 结构与状态时间线，
以及 `closed` 上 `confirm`（幂等）和 `update(open)`（409）两条错误路径，
44 项断言全部一致。

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
internal/db/db.go          SQLite 只读 / 读写两条连接
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
  transition.go            状态机：流转表、校验与错误码
  write.go                 confirm / cancel / update_status
```
