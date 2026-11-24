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
	"airetrieval/internal/search"
)

const (
	defaultAddr     = "127.0.0.1:8081"
	shutdownTimeout = 10 * time.Second

	// 入库 Worker 的并发度与队列缓冲：并发度对应 CPU 密集的分块计算，
	// 队列缓冲用于吸收突发请求，满了会让提交方等待（背压）。
	ingestWorkers   = 4
	ingestQueueSize = 64
)

// ingestWorker 是进程级的入库任务处理器，随服务启停。
var ingestWorker *ingest.Worker

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	ingestWorker = ingest.New(ingestWorkers, ingestQueueSize)

	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = defaultAddr
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("POST /search", handleSearch)
	mux.HandleFunc("POST /ingest", handleIngest)

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
		// 停止入库 Worker：等待在途分块任务处理完
		ingestWorker.Close()
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
			results[i], errs[i] = ingestWorker.Submit(r.Context(), job)
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

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Error("响应编码失败", "err", err)
	}
}
