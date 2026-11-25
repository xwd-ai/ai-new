package main

import (
	"database/sql"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
)

func publicPendingTickets(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		query := r.URL.Query()
		includeExpired, valid := pendingBool(query.Get("include_expired"), query.Has("include_expired"))
		allUsers, validScope := pendingBool(query.Get("all_users"), query.Has("all_users"))
		if !valid || !validScope {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "工单查询参数不合法"})
			return
		}
		var timeout *int
		if query.Has("timeout_seconds") {
			value, err := strconv.Atoi(strings.TrimSpace(query.Get("timeout_seconds")))
			if err != nil {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "待确认超时阈值必须是整数"})
				return
			}
			timeout = &value
		}

		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		userID := user.ID
		if timeout != nil && *timeout < 0 {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
				"error": "待确认超时阈值不能为负数", "error_code": "TICKET_INVALID_PAYLOAD",
				"error_detail": map[string]any{"field": "timeout_seconds", "value": *timeout},
			})
			return
		}
		if allUsers && user.IsAdmin {
			userID = ""
		}
		result, err := ticketRepo.ListPending(userID, includeExpired, timeout, nil)
		if err != nil {
			slog.Error("查询待确认工单失败", "err", err)
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "查询工单失败"})
			return
		}
		total, expired := result["total"].(int), result["expired"].(int)
		message := "暂无待确认工单"
		if total > 0 {
			message = fmt.Sprintf("共 %d 个待确认工单", total)
			if expired > 0 {
				message += fmt.Sprintf("，其中 %d 个已超时", expired)
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"tickets": result["items"], "total": total, "expired": expired,
			"timeout_seconds": result["timeout_seconds"], "message": message,
		})
	}
}

func pendingBool(raw string, present bool) (value, valid bool) {
	if !present {
		return false, true
	}
	switch strings.ToLower(raw) {
	case "1", "on", "t", "true", "y", "yes":
		return true, true
	case "0", "off", "f", "false", "n", "no":
		return false, true
	default:
		return false, false
	}
}
