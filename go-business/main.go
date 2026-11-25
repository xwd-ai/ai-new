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
	"strconv"
	"strings"
	"syscall"
	"time"

	"aibusiness/internal/auth"
	"aibusiness/internal/db"
	"aibusiness/internal/orders"
	"aibusiness/internal/tickets"
)

const (
	defaultAddr     = "127.0.0.1:8082"
	shutdownTimeout = 10 * time.Second
)

var (
	repo         *orders.Repository
	ticketRepo   *tickets.Repository
	ticketWriter *tickets.Writer
)

func main() {
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))

	// 查询用只读连接：物理上写不了，权限边界落在连接上
	conn, err := db.Open()
	if err != nil {
		slog.Error("数据库连接失败", "err", err)
		os.Exit(1)
	}
	defer conn.Close()

	// 写操作单独用读写连接：只有工单流转这条路径需要写权限
	writeConn, err := db.OpenWrite()
	if err != nil {
		slog.Error("数据库写连接失败", "err", err)
		os.Exit(1)
	}
	defer writeConn.Close()

	repo = orders.NewRepository(conn)
	ticketRepo = tickets.NewRepository(conn)
	ticketWriter = tickets.NewWriter(writeConn)

	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = defaultAddr
	}

	mux := http.NewServeMux()
	chat := newPublicChat(chatStore{read: conn, write: writeConn})
	mux.HandleFunc("GET /public/api/conversation", chat.handleConversation)
	mux.HandleFunc("POST /public/api/chat", func(w http.ResponseWriter, r *http.Request) {
		chat.respond(w, r, false)
	})
	mux.HandleFunc("POST /public/api/chat/stream", func(w http.ResponseWriter, r *http.Request) {
		chat.respond(w, r, true)
	})
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /public/api/me", publicAccount(conn))
	mux.HandleFunc("POST /public/api/login", publicLogin(writeConn))
	mux.HandleFunc("POST /public/api/register", publicRegister(writeConn))
	mux.HandleFunc("GET /public/api/sources/{source_id}", publicSource(conn))
	mux.HandleFunc("GET /public/api/knowledge/items", publicKnowledgeItems(conn))
	mux.HandleFunc("GET /public/api/knowledge/settings", publicRetrievalSettings(conn))
	mux.HandleFunc("PUT /public/api/knowledge/settings", publicRetrievalSettings(conn))
	mux.HandleFunc("GET /public/api/knowledge/ingest/pending", publicPendingIngests(conn))
	mux.HandleFunc("GET /public/api/order", publicOrder(conn, "primary"))
	mux.HandleFunc("GET /public/api/order/list", publicOrder(conn, "list"))
	mux.HandleFunc("GET /public/api/order/scope", publicOrder(conn, "scope"))
	mux.HandleFunc("GET /public/api/order/feature-scope", publicOrder(conn, "feature"))
	mux.HandleFunc("GET /public/api/order/renewal", publicOrder(conn, "renewal"))
	mux.HandleFunc("GET /public/api/order/admin", publicOrderAdminList(conn))
	mux.HandleFunc("GET /public/api/order/admin/{order_id}", publicOrderAdminDetail(conn))
	mux.HandleFunc("POST /public/api/tickets/reply", publicTicketReply(conn))
	mux.HandleFunc("GET /public/api/tickets", publicTicketList(conn))
	mux.HandleFunc("GET /public/api/tickets/stats", publicTicketStats(conn))
	mux.HandleFunc("GET /public/api/tickets/{ticket_id}", publicTicketDetail(conn))
	mux.HandleFunc("GET /public/api/tickets/{ticket_id}/transfer-decision", publicTicketTransferDecision(conn))
	mux.HandleFunc("POST /public/api/tickets/transfer", publicTicketTransfer(conn))
	mux.HandleFunc("POST /public/api/order/expire", publicOrderExpiration(conn, writeConn))
	mux.HandleFunc("POST /public/api/tickets/expire", publicTicketExpiration(conn))
	mux.HandleFunc("POST /public/api/tickets/batch-close", publicTicketBatch(conn, false))
	mux.HandleFunc("POST /public/api/tickets/batch-transfer", publicTicketBatch(conn, true))
	mux.HandleFunc("POST /public/api/logout", publicLogout(writeConn))
	mux.HandleFunc("POST /public/api/sessions/cleanup", publicSessionCleanup(conn, writeConn))
	mux.HandleFunc("POST /public/api/refresh", publicRefresh(writeConn))
	mux.HandleFunc("GET /api/order", handleOrder)
	mux.HandleFunc("GET /api/orders", handleOrders)
	mux.HandleFunc("GET /api/auth/verify", handleVerifyToken)
	mux.HandleFunc("GET /api/tickets/pending", handlePendingTickets)
	mux.HandleFunc("GET /public/api/tickets/pending", publicPendingTickets(conn))
	mux.HandleFunc("POST /public/api/tickets", publicTicketCreate(conn))
	for _, operation := range []string{"confirm", "cancel", "status"} {
		mux.HandleFunc("POST /public/api/tickets/"+operation, publicTicketWrite(conn, operation))
	}
	mux.HandleFunc("POST /api/tickets/confirm", handleConfirmTicket)
	mux.HandleFunc("POST /api/tickets/cancel", handleCancelTicket)
	mux.HandleFunc("POST /api/tickets/status", handleUpdateTicketStatus)

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

// handlePendingTickets 查询待确认草稿列表，对应 Python 的 list_pending_tickets。
func handlePendingTickets(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	userID := strings.TrimSpace(query.Get("user_id"))
	includeExpired := query.Get("include_expired") == "true" || query.Get("include_expired") == "1"

	var timeout *int
	if raw := strings.TrimSpace(query.Get("timeout_seconds")); raw != "" {
		value, err := strconv.Atoi(raw)
		// 负数阈值会把所有草稿判成超时，与 Python 一样当场拒绝而不是拉回 0
		if err != nil || value < 0 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "timeout_seconds 必须是非负整数"})
			return
		}
		timeout = &value
	}

	data, err := ticketRepo.ListPending(userID, includeExpired, timeout, nil)
	if err != nil {
		slog.Error("查询待确认工单失败", "err", err, "user_id", userID)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询工单失败"})
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

// ticketWriteRequest 是三个写端点共用的请求体。
//
// 三个动作的字段互不冲突、数量都很少，共用一个结构体比定义三个几乎一样的
// 类型更省事；多出来的字段自然被忽略，不需要额外校验「这个字段不该出现」。
type ticketWriteRequest struct {
	TicketID string `json:"ticket_id"`
	Operator string `json:"operator"`
	Reason   string `json:"reason"`
	Status   string `json:"status"`
}

func decodeWriteRequest(w http.ResponseWriter, r *http.Request) (ticketWriteRequest, bool) {
	var req ticketWriteRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error":      "请求体不是合法 JSON",
			"error_code": string(tickets.CodeInvalidPayload),
		})
		return req, false
	}
	return req, true
}

// handleConfirmTicket 确认待确认草稿，对应 Python 的 tickets.confirm_ticket。
func handleConfirmTicket(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeWriteRequest(w, r)
	if !ok {
		return
	}
	ticket, err := ticketWriter.Confirm(r.Context(), req.TicketID, req.Operator)
	writeTicketResult(w, req.TicketID, ticket, err)
}

// handleCancelTicket 取消待确认草稿，对应 Python 的 tickets.cancel_ticket。
func handleCancelTicket(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeWriteRequest(w, r)
	if !ok {
		return
	}
	ticket, err := ticketWriter.Cancel(r.Context(), req.TicketID, req.Reason, req.Operator)
	writeTicketResult(w, req.TicketID, ticket, err)
}

// handleUpdateTicketStatus 改工单状态，对应 Python 的 tickets.update_status。
//
// 操作人固定为 system，不接受请求体指定：这个端点是管理端的显式流转，
// 与「谁点的确认」不同，人工操作人应由上层审计日志记录，而不是混进时间线。
func handleUpdateTicketStatus(w http.ResponseWriter, r *http.Request) {
	req, ok := decodeWriteRequest(w, r)
	if !ok {
		return
	}
	ticket, err := ticketWriter.UpdateStatus(r.Context(), req.TicketID, req.Status)
	writeTicketResult(w, req.TicketID, ticket, err)
}

// writeTicketResult 把写操作结果映射成与 Python 一致的 HTTP 响应。
//
// 三档区分是刻意的，Python 侧据此还原成同一套异常类型：
//   - 404 工单不存在；
//   - 422 入参里的状态值不认识（调用方传错了）；
//   - 409 状态值合法但这条流转不允许（业务冲突）。
//
// 混用会导致前端无法区分「我传错了参数」和「这个动作现在不能做」。
func writeTicketResult(w http.ResponseWriter, ticketID string, ticket map[string]any, err error) {
	if err == nil {
		writeJSON(w, http.StatusOK, ticket)
		return
	}

	if errors.Is(err, tickets.ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]any{
			"error":        "工单不存在",
			"error_code":   string(tickets.CodeNotFound),
			"error_detail": map[string]any{"ticket_id": ticketID},
		})
		return
	}

	var transitionErr *tickets.TransitionError
	if errors.As(err, &transitionErr) {
		code := http.StatusConflict
		if transitionErr.Code == tickets.CodeUnknownStatus {
			code = http.StatusUnprocessableEntity
		}
		writeJSON(w, code, map[string]any{
			"error":      transitionErr.Message,
			"error_code": string(transitionErr.Code),
			"error_detail": map[string]any{
				"from_status": transitionErr.From,
				"to_status":   transitionErr.To,
			},
		})
		return
	}

	slog.Error("工单流转失败", "err", err, "ticket_id", ticketID)
	writeJSON(w, http.StatusInternalServerError, map[string]any{
		"error":      "工单操作失败",
		"error_code": "TICKET_INTERNAL",
	})
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		slog.Error("响应编码失败", "err", err)
	}
}
