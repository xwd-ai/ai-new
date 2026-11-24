# Go 网关

把 Python 后端与两个 Go 服务收在同一个对外入口后面。定位是**入口**而不是**业务**：
它不改写业务语义，只做几件在入口处才高效的事 —— 反向代理、入口鉴权、限流、
SSE 转发、超时、pprof、优雅关闭。

## 为什么单独一层

代码按能力边界拆分之后，对外入口需要一个统一的地方来放置横切关注点：

| 关注点 | 放在网关的理由 |
| --- | --- |
| 反向代理 | 前端只认一个地址，后端可以随意换端口 / 换实例 |
| 入口鉴权 | 每个请求统一校验一次，无效令牌在日志里可见 |
| 限流 | 在请求进入业务之前拦掉，不消耗后端资源 |
| SSE 转发 | 需要 `FlushInterval = -1`，只有代理层能控制刷写时机 |
| pprof | 剖析的是「入口到后端」整条链路的开销 |
| 优雅关闭 | 停止接单与等待在途请求是入口的职责 |

## 中间件顺序

```
访问日志 → CORS → 入口鉴权 → 入口限流 → 业务
```

顺序有两处必须如此：

- **日志在最外**：一个请求出任何问题都能留下完整记录，包括 panic 与 502。
- **鉴权在限流之外**：限流按用户 / IP 分桶，需要先拿到鉴权注入的身份；
  反过来就会出现「所有请求都按匿名桶计数」。

## 端点

| 端点 | 说明 |
| --- | --- |
| `GET /health` | 聚合 Python 与两个 Go 服务的健康状态，并透传前端依赖的 `stream` / `models` |
| `POST /api/retrieval/stream` | 转发到检索服务的 SSE，按阶段推送检索进度（请求体上限 4 MiB） |
| `GET /debug/pprof/*` | 仅在 `GATEWAY_PPROF=1` 时挂载 |
| 其余路径 | 全部反向代理到 `BACKEND_URL` |

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ADDR` | `127.0.0.1:8080` | 监听地址 |
| `BACKEND_URL` | `http://127.0.0.1:8000` | 必需，Python 后端地址 |
| `RETRIEVAL_URL` | 空 | 配置后启用 `/api/retrieval/stream` |
| `GO_BUSINESS_URL` | 空 | 配置后启用入口鉴权（经业务服务 `/api/auth/verify` 验签） |
| `GATEWAY_RATE_LIMIT` | `0` | 每秒补充的令牌数，`0` 表示关闭入口限流 |
| `GATEWAY_RATE_BURST` | 同 rate | 桶容量 |
| `GATEWAY_PPROF` | `false` | 是否暴露 `/debug/pprof` |
| `GATEWAY_UPSTREAM_TIMEOUT` | `30s` | 等待上游响应头的超时（不影响 SSE 的流式 body） |

## 设计取舍

**不接管授权判定。** 网关只做入口层令牌校验，结果用于日志与限流维度，
不写进转发给 Python 的请求头 —— 一旦 Python 信任自定义身份头，
它被直接暴露时那个头就成了可伪造的身份来源。授权仍只有 Python 一处权威，
网关只是多一层校验，不是多一个信任源。

**鉴权失败不阻断请求。** 无效令牌照常转发：匿名可访问的接口应当正常工作，
需要登录的接口由 Python 统一返回 401，错误格式才不会出现两套。
业务服务不可用时同样按匿名放行 —— 鉴权服务抖动不该变成业务整体不可用。

**限流默认关闭。** Python 侧已有固定窗口限流，两层同时生效会让实际阈值减半。
网关限流是「入口粗限流」，启用时应把 Python 侧阈值同步调高，两者定位不同。

**CORS 只在预检时处理。** 若网关与 Python 都设置 `Access-Control-Allow-Origin`，
反向代理会把它们合并成两个值，浏览器直接判定跨域失败。

## 目录

```
go-gateway/
├── main.go                  入口、中间件串联、反向代理、SSE 转发、健康聚合
└── internal/
    ├── httplog/             slog 结构化访问日志、X-Request-ID、panic 兜底
    ├── ratelimit/           x/time/rate token bucket，按 key 分桶 + 空闲回收
    ├── singleflight/        x/sync/singleflight 的泛型封装（支持 ctx 取消）
    └── distlock/            Redis SET NX PX + Lua 比对删除（与 Python 同协议）
```

`distlock` 当前没有被网关调用：网关不写数据。它抽出来是为了让「比对 token 再删除」
这套锁协议在 Go 侧有可复用实现，配套单测用 miniredis 验证互斥、TTL 与误删保护。

## 本地运行

```bash
# 先在另一个终端启动 Python 后端（默认 127.0.0.1:8000）
cd go-gateway && go run .
```

```bash
# 健康检查：应看到 gateway=true 与各上游状态
curl -s http://127.0.0.1:8080/health | python -m json.tool

# 检索阶段流（需先配置 RETRIEVAL_URL 并启动 go-retrieval）
curl -N -X POST http://127.0.0.1:8080/api/retrieval/stream \
  -H 'Content-Type: application/json' \
  -d '{"query":"导出失败","top_k":3,"docs":[{"id":"k-1","title":"导出失败","content":"检查授权是否过期"}]}'
```

## 测试

```bash
go test ./... -race
```
