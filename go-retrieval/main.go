// 检索服务：把「关键词 + 向量双路召回 → RRF 融合」独立成一个 Go 进程。
//
// 服务刻意不持有业务状态：文档集合由调用方随请求传入，服务只负责计算与排序。
// 这样它可以单独压测、单独扩容，也能被任何语言的后端调用。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"airetrieval/internal/ingest"
	"airetrieval/internal/rank"
	"airetrieval/internal/search"
	"airetrieval/internal/stream"
)

const (
	defaultAddr     = "127.0.0.1:8081"
	shutdownTimeout = 10 * time.Second

	// 入库 Worker 的并发度与队列缓冲：并发度对应 CPU 密集的分块计算，
	// 队列缓冲用于吸收突发请求，满了会让提交方等待（背压）。
	ingestWorkers   = 4
	ingestQueueSize = 64
)

// ingestPipeline 是进程级的入库流水线：parse → chunk → 指纹。
//
// 相对早先的单层 Worker，它补上了逐任务取消、检查点与优雅关闭：
// 客户端断开或关闭信号能让在途任务尽快停下，而不是继续算完再丢弃。
// 落库仍由调用方完成，本服务只做 CPU 密集的预处理。
var ingestPipeline *ingest.Pipeline

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	ingestPipeline = ingest.NewPipeline(ingest.PipelineConfig{
		Workers:   ingestWorkers,
		QueueSize: ingestQueueSize,
	})

	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = defaultAddr
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("POST /search", handleSearch)
	mux.HandleFunc("POST /stream", handleStream)
	mux.HandleFunc("POST /ingest", handleIngest)
	mux.HandleFunc("GET /ingest/stats", handleIngestStats)
	mux.HandleFunc("POST /rank", handleRank)

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	// 优雅关闭：收到信号后停止接受新连接，并给在途请求留出完成时间
	go func() {
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
		<-stop

		ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			slog.Error("服务关闭异常", "err", err)
		}
		// 停止入库流水线：不再接单，等待在途任务处理完并释放租约
		ingestPipeline.Close()
	}()

	slog.Info("检索服务启动", "addr", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("服务启动失败", "err", err)
		os.Exit(1)
	}
	slog.Info("检索服务已退出")
}

func handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// searchRequest 是检索请求体。
//
// 文档随请求传入而不是在服务内维护：本服务不持有业务状态，
// 调用方（Python 后端）决定检索哪批文档，服务只做计算与排序。
type searchRequest struct {
	Query string            `json:"query"`
	TopK  int               `json:"top_k"`
	Docs  []search.Document `json:"docs"`
}

type searchResponse struct {
	Results []search.Result `json:"results"`
	Count   int             `json:"count"`
}

const defaultTopK = 3

func handleSearch(w http.ResponseWriter, r *http.Request) {
	var req searchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体不是合法 JSON"})
		return
	}
	if strings.TrimSpace(req.Query) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "query 不能为空"})
		return
	}
	if req.TopK <= 0 {
		req.TopK = defaultTopK
	}

	// 传入 r.Context()：客户端断开或超时会沿调用链取消下游计算
	searcher := &search.Searcher{Docs: req.Docs}
	results, err := searcher.Search(r.Context(), req.Query, req.TopK)
	if err != nil {
		slog.Error("检索失败", "err", err, "query", req.Query)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "检索失败"})
		return
	}

	writeJSON(w, http.StatusOK, searchResponse{Results: results, Count: len(results)})
}

// handleStream 以 SSE 形式把检索过程按阶段实时推给客户端。
//
// 与 /search 的区别只在「交付方式」：/search 等全部算完一次返回，
// /stream 每完成一个阶段就推一条事件，前端可以立刻显示进度。
// 用 POST 而不是 GET，是因为文档集合随请求体传入、体积可能较大。
func handleStream(w http.ResponseWriter, r *http.Request) {
	var req searchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体不是合法 JSON"})
		return
	}
	if strings.TrimSpace(req.Query) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "query 不能为空"})
		return
	}
	if req.TopK <= 0 {
		req.TopK = defaultTopK
	}

	sse, err := stream.NewWriter(w)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	ctx := r.Context()
	started := time.Now()
	eventID := 0

	// 客户端断开后 emit 返回 false，后续阶段应立即停止计算而不是继续白跑
	emit := func(event string, payload any) bool {
		if ctx.Err() != nil {
			return false
		}
		if err := sse.Send(eventID, event, payload); err != nil {
			return false
		}
		eventID++
		return true
	}

	// 心跳保活：长时间无事件时避免被代理或负载均衡当作空闲连接断开
	stopHeartbeat := make(chan struct{})
	defer close(stopHeartbeat)
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stopHeartbeat:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				_ = sse.Comment("keep-alive")
			}
		}
	}()

	if !emit("start", map[string]any{"query": req.Query, "docs": len(req.Docs)}) {
		return
	}

	prefetch := req.TopK * 5

	keywordHits := search.KeywordSearch(req.Query, req.Docs, prefetch)
	if !emit("keyword", map[string]any{"hits": len(keywordHits)}) {
		return
	}

	// 本服务未注入 Embedder：如实标注跳过，而不是假装做过向量检索
	if !emit("vector", map[string]any{"skipped": true, "reason": "未配置 Embedder"}) {
		return
	}

	fused := search.FuseRRF(keywordHits, nil, req.TopK, search.DefaultRRFK)
	if !emit("result", map[string]any{"count": len(fused), "items": fused}) {
		return
	}

	emit("done", map[string]any{"elapsed_ms": time.Since(started).Milliseconds()})
}

// ingestRequest 是批量入库预处理请求。
//
// 服务只做「分块 + 指纹」这类 CPU 密集的预处理，落库仍由调用方完成，
// 因此它不持有任何存储状态。
type ingestRequest struct {
	Docs []ingest.Job `json:"docs"`
}

type ingestResponse struct {
	Results []ingest.Result `json:"results"`
	Count   int             `json:"count"`
}

func handleIngest(w http.ResponseWriter, r *http.Request) {
	var req ingestRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体不是合法 JSON"})
		return
	}
	if len(req.Docs) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "docs 不能为空"})
		return
	}
	for _, job := range req.Docs {
		if job.ID == "" || strings.TrimSpace(job.Content) == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "每个文档都需要 id 与非空 content"})
			return
		}
	}

	// 并发提交给 Worker：并发度由 Worker 的 goroutine 数量控制，
	// 这里不另起无限协程，队列满时 Submit 会阻塞形成背压。
	results := make([]ingest.Result, len(req.Docs))
	errs := make([]error, len(req.Docs))
	var wg sync.WaitGroup

	for i, job := range req.Docs {
		wg.Add(1)
		go func(i int, job ingest.Job) {
			defer wg.Done()
			results[i], errs[i] = ingestPipeline.Submit(r.Context(), job)
		}(i, job)
	}
	wg.Wait()

	for _, err := range errs {
		if err != nil {
			slog.Error("入库预处理失败", "err", err)
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "入库服务暂不可用"})
			return
		}
	}

	writeJSON(w, http.StatusOK, ingestResponse{Results: results, Count: len(results)})
}

// handleIngestStats 暴露入库流水线的实时计数。
//
// 背压是否生效、失败率多少、有没有任务卡在队列里，这些在排查
// 「入库变慢」时最想知道。只读无副作用，可以安全地被运维轮询。
func handleIngestStats(w http.ResponseWriter, _ *http.Request) {
	s := ingestPipeline.Stats()
	writeJSON(w, http.StatusOK, map[string]int64{
		"queued":    s.Queued,
		"in_flight": s.InFlight,
		"completed": s.Completed,
		"failed":    s.Failed,
		"retried":   s.Retried,
	})
}

// rankRequest 是检索后处理请求。
//
// 重排模型本身仍留在调用方（模型客户端与密钥都在 Python 侧），
// 本服务只承担模型之后的确定性计算：复合分、阈值降级、MMR 去冗余。
// 调用方把模型分按「可打分候选」的顺序放进 rerank_scores；
// 不提供该字段表示「没有重排模型」，此时直接按召回分去冗余。
type rankRequest struct {
	Query        string         `json:"query"`
	Results      []rank.Item    `json:"results"`
	RerankScores []float64      `json:"rerank_scores"`
	TopK         int            `json:"top_k"`
	Threshold    *float64       `json:"threshold"`
	Lambda       *float64       `json:"lambda"`
	Config       map[string]any `json:"config"`
}

type rankResponse struct {
	Results     []rank.Item      `json:"results"`
	Count       int              `json:"count"`
	Diagnostics rank.Diagnostics `json:"diagnostics"`
}

func handleRank(w http.ResponseWriter, r *http.Request) {
	var req rankRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "请求体不是合法 JSON"})
		return
	}
	if req.TopK <= 0 {
		req.TopK = defaultTopK
	}
	if len(req.Results) == 0 {
		writeJSON(w, http.StatusOK, rankResponse{
			Results: []rank.Item{}, Count: 0, Diagnostics: rank.Diagnostics{},
		})
		return
	}
	// 空数组与缺省同样表示「没有模型分」：两者都不该被当成
	// 「传了分数但长度对不上」的错误映射。
	if len(req.RerankScores) == 0 {
		req.RerankScores = nil
	}

	cfg := rank.DefaultConfig()
	if req.Config != nil {
		cfg = rank.ParseConfig(req.Config)
	}

	results, diag := rank.Rank(req.Query, req.Results, req.RerankScores, rank.Options{
		TopK:      req.TopK,
		Threshold: req.Threshold,
		Lambda:    req.Lambda,
		Config:    cfg,
	})
	writeJSON(w, http.StatusOK, rankResponse{
		Results: results, Count: len(results), Diagnostics: diag,
	})
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Error("响应编码失败", "err", err)
	}
}
