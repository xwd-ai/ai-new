package main

import (
	"database/sql"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/tickets"
)

func requireTicketAdmin(conn *sql.DB, w http.ResponseWriter, r *http.Request) (ticketUser, bool) {
	user, ok := requireTicketUser(conn, w, r)
	if ok && !user.IsAdmin {
		writeSessionError(w, http.StatusForbidden, "TICKET_FORBIDDEN", "无权限")
		return user, false
	}
	return user, ok
}

func publicTicketReply(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketAdmin(conn, w, r)
		if !ok {
			return
		}
		fields, ok := decodePublicTicketBody(w, r)
		if !ok {
			return
		}
		id, validID := ticketString(fields["ticket_id"])
		content, validContent := ticketString(fields["content"])
		if !validID || id == "" || utf8.RuneCountInString(id) > 64 ||
			!validContent || strings.TrimSpace(content) == "" || utf8.RuneCountInString(content) > 2000 {
			writeTicketInputError(w, "工单号或回复内容不合法")
			return
		}
		result, err := ticketWriter.Reply(r.Context(), id, user.Name, strings.TrimSpace(content))
		if err != nil {
			writeTicketResult(w, id, nil, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ticket": result})
	}
}

func publicTicketExpiration(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireTicketAdmin(conn, w, r); !ok {
			return
		}
		query := r.URL.Query()
		timeout, limit := tickets.PendingTimeoutSeconds, 200
		var err error
		if query.Has("timeout_seconds") {
			timeout, err = strconv.Atoi(query.Get("timeout_seconds"))
			if err != nil || timeout < 0 {
				writeTicketInputError(w, "待确认超时阈值必须是非负整数")
				return
			}
		}
		if query.Has("limit") {
			limit, err = strconv.Atoi(query.Get("limit"))
			if err != nil || limit < 1 || limit > 500 {
				writeTicketInputError(w, "每批关闭条数必须为 1 到 500")
				return
			}
		}
		result, err := ticketWriter.ExpirePending(r.Context(), timeout, limit)
		if err != nil {
			slog.Error("关闭超时工单失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "TICKET_INTERNAL", "关闭超时工单失败")
			return
		}
		message := "没有需要关闭的超时待确认工单"
		if result.Closed > 0 {
			message = fmt.Sprintf("已关闭 %d 个超时待确认工单", result.Closed)
		}
		writeJSON(w, http.StatusOK, map[string]any{"result": result, "message": message})
	}
}

func publicTicketStats(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		query := r.URL.Query()
		userID := query.Get("user_id")
		if !user.IsAdmin {
			if userID != "" && userID != user.ID {
				writeSessionError(w, http.StatusForbidden, "TICKET_FORBIDDEN", "普通用户只能查看本人工单统计")
				return
			}
			userID = user.ID
		}
		var days *int
		if query.Has("days") {
			value, err := strconv.Atoi(query.Get("days"))
			if err != nil || value < 1 || value > 365 {
				writeSessionError(w, http.StatusUnprocessableEntity, "TICKET_INVALID_STATS_RANGE", "统计天数需在 1 到 365 之间")
				return
			}
			days = &value
		}
		stats, message, err := ticketRepo.Statistics(r.Context(), userID, days)
		if err != nil {
			slog.Error("读取工单统计失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "TICKET_INTERNAL", "读取工单统计失败")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"stats": stats, "message": message})
	}
}
