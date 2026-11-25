package main

import (
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"unicode/utf8"

	"aibusiness/internal/orders"
)

func publicOrder(conn *sql.DB, view string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		account, err := resolveAccount(conn, r)
		if errors.Is(err, sql.ErrNoRows) && view == "primary" {
			writeJSON(w, http.StatusOK, map[string]any{"order": nil})
			return
		}
		if errors.Is(err, sql.ErrNoRows) {
			writeSessionError(w, http.StatusUnauthorized, "TICKET_UNAUTHORIZED", "未登录")
			return
		}
		if err != nil {
			publicOrderError(w, err)
			return
		}
		var result map[string]any
		switch view {
		case "primary":
			var primary map[string]any
			primary, err = repo.QueryOrder(account.ID)
			result = map[string]any{"order": primary}
		case "list":
			result, err = repo.QueryOrders(account.ID, nil)
		case "feature":
			feature := r.URL.Query().Get("feature")
			if utf8.RuneCountInString(feature) > 50 {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "功能查询参数超长"})
				return
			}
			var rows []orders.Order
			rows, err = repo.List(account.ID)
			if err == nil {
				result = orders.EvaluateFeatureScope(rows, feature, nil)
			}
		case "renewal":
			window := 30
			if r.URL.Query().Has("window_days") {
				window, err = strconv.Atoi(r.URL.Query().Get("window_days"))
				if err != nil || window < 0 || window > 365 {
					writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "续费提醒窗口必须为 0 到 365 天"})
					return
				}
			}
			var rows []orders.Order
			rows, err = repo.List(account.ID)
			if err == nil {
				result = orders.RenewalHints(rows, window, nil)
			}
		case "scope":
			feature, id := r.URL.Query().Get("feature"), r.URL.Query().Get("order_id")
			if utf8.RuneCountInString(feature) > 50 || utf8.RuneCountInString(id) > 64 {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "订单范围查询参数超长"})
				return
			}
			var order *orders.Order
			order, err = repo.GetOrder(account.ID, id)
			if err == nil {
				result = publicOrderScope(orders.EvaluateServiceScope(order, feature, nil))
			}
		}
		if err != nil {
			publicOrderError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

func publicOrderScope(scope orders.Scope) map[string]any {

	return map[string]any{
		"has_order": scope.HasOrder, "order_id": scope.OrderID, "tier": scope.Tier,
		"tier_label": scope.TierLabel, "order_status": scope.OrderStatus, "period": scope.Period,
		"features": scope.Features, "feature": scope.Feature, "in_service": scope.InService,
		"allowed": scope.Allowed, "reason_code": scope.ReasonCode, "reason": scope.Reason, "message": orders.ScopeMessage(scope),
	}
}

func publicOrderError(w http.ResponseWriter, err error) {
	slog.Error("读取订单失败", "err", err)
	writeSessionError(w, http.StatusInternalServerError, "ORDER_ERROR", "读取订单失败")
}

func publicOrderExpiration(read, write *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireTicketAdmin(read, w, r); !ok {
			return
		}
		query := r.URL.Query()
		limit := 100
		if query.Has("limit") {
			value, err := strconv.Atoi(query.Get("limit"))
			if err != nil || value < 1 || value > 500 {
				writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "单次归档数量必须为 1 到 500"})
				return
			}
			limit = value
		}
		result, err := orders.ExpireOverdue(r.Context(), write, limit)
		if err != nil {
			publicOrderError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}
