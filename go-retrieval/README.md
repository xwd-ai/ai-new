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

## 设计要点

| 决策 | 理由 |
|---|---|
| 只用名次做融合（RRF） | 余弦相似度与 BM25 分数量纲不同，直接相加会让一路压倒另一路 |
| 向量路失败只降级 | 检索有结果比整体失败更有价值；退化为纯关键词结果 |
| 文档随请求传入 | 服务无状态，便于单独压测、扩容与替换 |
| `Embedder` 抽成接口 | 调用方可注入任意实现，测试用桩替换，不依赖网络与密钥 |

## 测试

```bash
go test ./... -race    # -race 打开竞态检测
```

覆盖：分词、BM25 排序与截断、余弦相似度与维度不一致、RRF 融合与归一化、
两路降级、context 取消、并发只读安全。

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
```
