// 业务服务：把订单与服务范围判定从 Python 迁移到 Go 的第一个模块。
//
// 定位与检索服务一致 —— 无状态、可独立部署、可被 Python 通过 HTTP 调用。
// 区别在于它**需要读数据库**，因此以只读连接打开，只承担查询职责；
// Python 侧保留完整实现作为降级路径。
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
	"syscall"
	"time"

	"aibusiness/internal/auth"
	"aibusiness/internal/db"
	"aibusiness/internal/orders"
)

const (
	defaultAddr     = "127.0.0.1:8082"
	shutdownTimeout = 10 * time.Second
)

var repo *orders.Repository

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	conn, err := db.Open()
	if err != nil {
		slog.Error("数据库连接失败", "err", err)
		os.Exit(1)
	}
	defer conn.Close()

	repo = orders.NewRepository(conn)

	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = defaultAddr
	}

	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /api/order", handleOrder)
	mux.HandleFunc("GET /api/orders", handleOrders)
	mux.HandleFunc("GET /api/auth/verify", handleVerifyToken)

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
		<-stop

		ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			slog.Error("服务关闭异常", "err", err)
		}
	}()

	slog.Info("业务服务启动", "addr", addr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("服务启动失败", "err", err)
		os.Exit(1)
	}
	slog.Info("业务服务已退出")
}

func handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// handleOrder 返回单个用户的主订单与服务范围，对应 Python 的 query_order。
func handleOrder(w http.ResponseWriter, r *http.Request) {
	userID := strings.TrimSpace(r.URL.Query().Get("user_id"))
	if userID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "user_id 不能为空"})
		return
	}

	data, err := repo.QueryOrder(userID)
	if err != nil {
		slog.Error("查询订单失败", "err", err, "user_id", userID)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询订单失败"})
		return
	}
	slog.Info("订单查询", "user_id", userID, "has_order", data["has_order"])
	writeJSON(w, http.StatusOK, data)
}

// handleOrders 返回用户全部订单与在期能力并集，对应 Python 的 query_orders。
func handleOrders(w http.ResponseWriter, r *http.Request) {
	userID := strings.TrimSpace(r.URL.Query().Get("user_id"))
	if userID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "user_id 不能为空"})
		return
	}

	data, err := repo.QueryOrders(userID, nil)
	if err != nil {
		slog.Error("查询订单列表失败", "err", err, "user_id", userID)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询订单失败"})
		return
	}
	writeJSON(w, http.StatusOK, data)
}

// handleVerifyToken 校验 access token，供 Python 或网关转发校验请求。
//
// token 从 Authorization 头读取而不是 query 参数：query 会进入访问日志与
// 浏览器历史，等于把凭据写进日志。
func handleVerifyToken(w http.ResponseWriter, r *http.Request) {
	raw := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
	if raw == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "缺少 Bearer 令牌"})
		return
	}

	claims, err := auth.VerifyAccessToken(raw)
	if err != nil {
		// 校验失败是业务结果而不是服务故障，仍返回 200，由 valid 字段表达
		writeJSON(w, http.StatusOK, map[string]any{"valid": false, "error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{"valid": true, "claims": claims})
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Error("响应编码失败", "err", err)
	}
}
