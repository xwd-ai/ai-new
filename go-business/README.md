# 业务服务（Go）

把 Python 后端里的**订单与服务范围判定**迁移到 Go 的第一个模块。

## 为什么先迁订单

- 依赖最干净：只读 `orders` / `products` 两张表，不涉及 LLM、事务、锁；
- 验收标准明确：返回结构必须与 Python 的 `orders.query_order` **逐字段一致**，
  可用固定输入直接比对；
- 失败可无损回滚：Python 侧保留完整实现，去掉环境变量即恢复原路径。

## 运行

```bash
go run .                                      # 默认 127.0.0.1:8082，读 ../backend/app.db
DB_PATH=/path/to/app.db ADDR=127.0.0.1:9000 go run .
```

数据库以**只读模式**打开（`mode=ro`）：当前阶段 Go 只承担查询，
从连接层面禁止写入，避免与 Python 争抢写权限。

## 接口

| 方法 | 路径 | 对应 Python |
|---|---|---|
| GET | `/health` | — |
| GET | `/api/order?user_id=x` | `orders.query_order` |
| GET | `/api/orders?user_id=x` | `orders.query_orders` |
| GET | `/api/auth/verify` | `auth.resolve_token`（仅校验层） |

`/api/auth/verify` 从 `Authorization: Bearer` 头读令牌，不接受 query 参数——
query 会进入访问日志与浏览器历史，等于把凭据写进日志。

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

`internal/orders/orders_test.go` 里固化了 Python `evaluate_service_scope`
在 9 种场景下的输出（在期、过期、未开始、退订、未知版本、起止颠倒、
功能不包含、功能无法识别、无订单），Go 实现必须逐字段匹配。

```bash
go test ./... -v
```

任何字段漂移（包括百分比舍入、日期格式、空数组与 null 的区分）都会让测试失败。

## 已知差异

- **时间精度**：Python 用 `time.time()`，Go 用 `UnixNano()/1e9`，纳秒级差异；
  判定结果按天数与百分比取整，不影响输出。
- **舍入规则**：Python 的 `round(x, 1)` 是四舍六入五成双，Go 的 `math.Round`
  是五远离零 —— 这里实现了 `roundHalfEven` 专门对齐。

## 目录

```
main.go                    HTTP 入口与路由
internal/db/db.go          SQLite 只读连接
internal/orders/
  types.go                 Order 与文本归一
  feature.go               版本能力矩阵与功能解析
  period.go                服务期判定（含舍入对齐）
  scope.go                 服务范围综合判定
  query.go                 取数与 query_order / query_orders
```
