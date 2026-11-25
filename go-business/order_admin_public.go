package main

import (
	"database/sql"
	"errors"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/orders"
)

func publicOrderAdminDetail(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireTicketAdmin(conn, w, r); !ok {
			return
		}
		id := r.PathValue("order_id")
		result, err := repo.AdminDetail(r.Context(), id)
		if errors.Is(err, sql.ErrNoRows) {
			writeJSON(w, http.StatusNotFound, map[string]any{
				"error": "订单不存在", "error_code": "ORDER_NOT_FOUND",
				"error_detail": map[string]string{"order_id": id},
			})
			return
		}
		if err != nil {
			publicOrderError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

func publicOrderAdminList(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if _, ok := requireTicketAdmin(conn, w, r); !ok {
			return
		}
		q := r.URL.Query()
		filter := orders.AdminFilter{Statuses: []string{}, Tiers: []string{}, Page: 1, Size: 20}
		for _, field := range []string{"status", "tier"} {
			for _, raw := range q[field] {
				for item := range strings.SplitSeq(raw, ",") {
					item = strings.TrimSpace(item)
					if item == "" || strings.EqualFold(item, "all") {
						continue
					}
					if field == "tier" {
						tier, valid := orders.NormalizeTier(item)
						if !valid {
							orderFilterError(w, "未知的产品版本："+item, field, item)
							return
						}
						if !slices.Contains(filter.Tiers, tier) {
							filter.Tiers = append(filter.Tiers, tier)
						}
						continue
					}
					status := strings.ToLower(item)
					if status != "active" && status != "expired" && status != "cancelled" {
						orderFilterError(w, "未知的订单状态："+item, field, item)
						return
					}
					if !slices.Contains(filter.Statuses, status) {
						filter.Statuses = append(filter.Statuses, status)
					}
				}
			}
		}
		filter.UserID = strings.Join(strings.Fields(q.Get("user_id")), " ")
		filter.Keyword = strings.Join(strings.Fields(q.Get("keyword")), " ")
		if utf8.RuneCountInString(filter.UserID) > 64 {
			orderFilterError(w, "客户编号不能超过 64 个字符", "user_id", "")
			return
		}
		if utf8.RuneCountInString(filter.Keyword) > 64 {
			orderFilterError(w, "关键词不能超过 64 个字符", "keyword", "")
			return
		}
		var err error
		if q.Has("page") {
			filter.Page, err = strconv.Atoi(q.Get("page"))
			if err != nil || filter.Page < 1 {
				writeJSON(w, 422, map[string]string{"detail": "页码必须大于等于 1"})
				return
			}
		}
		if q.Has("page_size") {
			filter.Size, err = strconv.Atoi(q.Get("page_size"))
			if err != nil || filter.Size < 1 || filter.Size > 100 {
				writeJSON(w, 422, map[string]string{"detail": "每页条数必须为 1 到 100"})
				return
			}
		}
		switch strings.ToLower(strings.TrimSpace(q.Get("order"))) {
		case "asc", "ascending":
			filter.Direction = "asc"
		case "", "desc", "descending":
			filter.Direction = "desc"
		default:
			orderFilterError(w, "排序方向只支持 asc 或 desc", "order", "")
			return
		}
		result, err := repo.AdminList(r.Context(), filter)
		if err != nil {
			publicOrderError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

func orderFilterError(w http.ResponseWriter, message, field, value string) {
	detail := map[string]string{"field": field}
	if value != "" {
		detail["value"] = value
	}
	writeJSON(w, http.StatusBadRequest, map[string]any{
		"error": message, "error_code": "ORDER_INVALID_FILTER", "error_detail": detail,
	})
}
