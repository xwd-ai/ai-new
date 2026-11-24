# 检索服务（Go）

把 Python 后端里的「关键词 + 向量双路召回 → RRF 融合」独立成一个 Go 进程。

## 为什么用 Go

- **双路召回天然可并行**：关键词检索与向量检索互不依赖，用 `errgroup` 并行后，
  总耗时约等于较慢那一路，而不是两路之和；
- **`context` 贯穿**：请求超时或客户端断开时，沿调用链取消下游计算
  （真实 `Embedder` 需响应 `ctx.Done()`）；
- **单二进制部署**：无运行时依赖，镜像小、启动快。

## 运行

```bash
go run .                                  # 默认 127.0.0.1:8081
ADDR=127.0.0.1:9000 go run .              # 指定监听地址
```

## 接口

### `GET /health`

```json
{"status":"ok"}
```

### `POST /search`

文档随请求传入 —— 服务不持有业务状态，调用方决定检索哪批文档。

请求：

```json
{
  "query": "导出失败",
  "top_k": 2,
  "docs": [
    {"id": "k-export", "title": "导出功能失败", "content": "导出 PDF 点击无反应",
     "keywords": ["导出", "PDF"]},
    {"id": "k-refund", "title": "退款政策", "content": "七天无理由退款",
     "keywords": ["退款"]}
  ]
}
```

响应：

```json
{
  "results": [
    {"id": "k-export", "title": "导出功能失败", "content": "导出 PDF 点击无反应",
     "score": 1, "match_type": "keyword"}
  ],
  "count": 1
}
```

`match_type` 取值：`keyword`（仅关键词命中）、`vector`（仅向量命中）、
`hybrid`（两路都命中，融合后排序更靠前）。

错误：`query` 为空或请求体非法返回 `400`。

### `POST /stream`

以 SSE 把检索过程按阶段实时推送。请求体与 `/search` 相同，
区别只在交付方式：`/search` 等全部算完一次返回，`/stream` 每完成一个阶段推一条事件。

```bash
curl -N -X POST http://127.0.0.1:8081/stream \
  -H "Content-Type: application/json" \
  -d '{"query":"导出失败","top_k":2,"docs":[...]}'
```

```
id: 0
event: start
data: {"docs":2,"query":"导出失败"}

id: 1
event: keyword
data: {"hits":1}

id: 2
event: vector
data: {"reason":"未配置 Embedder","skipped":true}

id: 3
event: result
data: {"count":1,"items":[...]}

id: 4
event: done
data: {"elapsed_ms":0}
```

用 POST 而非 GET，是因为文档集合随请求体传入、体积可能较大；
客户端需用 `fetch` 读流而不是 `EventSource`。

### `POST /ingest`

入库预处理：把文档切成可检索的分块并计算去重指纹。
落库仍由调用方完成，服务不持有存储状态。

请求：

```json
{
  "docs": [
    {"id": "d1", "title": "导出排查",
     "content": "第一段：检查权限。\n\n第二段：检查磁盘空间。"}
  ]
}
```

响应：

```json
{
  "results": [
    {"id": "d1",
     "chunks": [{"index": 0, "text": "第一段：检查权限。\n\n第二段：检查磁盘空间。"}],
     "fingerprint": "267efa6c9957ca48..."}
  ],
  "count": 1
}
```

分块策略：先按空行分段，再贪心拼接到不超过 500 字符；
单段超长时按字符硬切（不切断多字节字符）。指纹对「标题 + 正文」
做空白规范化后取 SHA-256，用于识别重复入库。

错误：`docs` 为空或缺少 `id` / `content` 返回 `400`；Worker 已关闭返回 `503`。

## 设计要点

| 决策 | 理由 |
|---|---|
| 只用名次做融合（RRF） | 余弦相似度与 BM25 分数量纲不同，直接相加会让一路压倒另一路 |
| 向量路失败只降级 | 检索有结果比整体失败更有价值；退化为纯关键词结果 |
| 文档随请求传入 | 服务无状态，便于单独压测、扩容与替换 |
| `Embedder` 抽成接口 | 调用方可注入任意实现，测试用桩替换，不依赖网络与密钥 |
| 入库用固定 worker pool | 分块是 CPU 密集任务，并发度必须可控；突然涌入时靠有界队列形成背压 |
| 关闭时用 `ctx` 广播而非 `close(channel)` | 关闭 channel 后向其发送会 panic，而「提交」与「关闭」无法靠 channel 关闭安全协调 |
| 按字符而非字节切块 | 中文一个字符三字节，按字节切会切坏多字节字符 |
| SSE 每条消息后都 Flush | 不 Flush 数据会积在缓冲里，客户端看不到「流」的效果 |
| SSE 写操作加锁 | 心跳协程与业务协程并发写同一 ResponseWriter，交错写会拼出非法帧 |
| 心跳用注释行 | SSE 规范中冒号开头的行被客户端忽略，但能维持连接活跃 |
| 客户端断开即停止计算 | 每阶段前检查 `ctx.Err()`，避免给已断开的连接白算 |

## 测试

```bash
go test ./... -race    # -race 打开竞态检测
```

覆盖：分词、BM25 排序与截断、余弦相似度与维度不一致、RRF 融合与归一化、
两路降级、context 取消、并发只读安全；分块边界、指纹规范化、
Worker 并发提交、关闭后拒绝新任务、关闭等待在途任务。

## 目录

```
main.go                     服务入口：路由、优雅关闭
internal/search/
  types.go                  Document / Result
  tokenize.go               分词与文档词元组装
  keyword.go                BM25 打分与归一化
  vector.go                 余弦相似度
  rrf.go                    RRF 融合
  service.go                并行召回编排（errgroup）
internal/ingest/
  chunk.go                  分块与指纹
  worker.go                 worker pool：有界队列、背压、优雅关闭
internal/stream/
  sse.go                    SSE 写入：Flush、心跳、并发安全
```
