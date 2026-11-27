package main

import (
	"database/sql"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/tickets"
)

func publicTicketList(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		q := r.URL.Query()
		filter := tickets.ListFilter{UserID: user.ID, Page: 1, Size: 20, Statuses: []string{}}
		if user.IsAdmin {
			filter.UserID = ""
		}
		if q.Has("conversation_id") {
			filter.ConversationID = q.Get("conversation_id")
			if filter.ConversationID == "" || len(filter.ConversationID) > 64 {
				writeTicketInputError(w, "会话 ID 不合法")
				return
			}
		}
		for _, entry := range q["status"] {
			for value := range strings.SplitSeq(entry, ",") {
				status := strings.ToLower(strings.TrimSpace(value))
				if status == "" || status == "all" {
					continue
				}
				if !tickets.IsValidStatus(status) {
					writeSessionError(w, 422, "TICKET_INVALID_STATUS_FILTER", "未知的工单状态："+status)
					return
				}
				if !slices.Contains(filter.Statuses, status) {
					filter.Statuses = append(filter.Statuses, status)
				}
			}
		}
		var err error
		if q.Has("page") {
			filter.Page, err = strconv.Atoi(q.Get("page"))
			if err != nil || filter.Page < 1 {
				writeSessionError(w, 422, "TICKET_INVALID_PAGINATION", "页码必须大于等于 1")
				return
			}
		}
		if q.Has("page_size") {
			filter.Size, err = strconv.Atoi(q.Get("page_size"))
			if err != nil || filter.Size < 1 || filter.Size > 100 {
				writeSessionError(w, 422, "TICKET_INVALID_PAGINATION", "每页条数必须为 1 到 100")
				return
			}
		}
		switch strings.ToLower(strings.TrimSpace(q.Get("order"))) {
		case "asc", "ascending", "created_asc":
			filter.Direction = "asc"
		case "", "desc", "descending", "created_desc":
			filter.Direction = "desc"
		default:
			writeSessionError(w, 422, "TICKET_INVALID_ORDER", "排序方向只支持 asc 或 desc")
			return
		}
		keyword := q.Get("keyword")
		if !q.Has("keyword") {
			keyword = q.Get("q")
		}
		filter.Keyword = strings.Join(strings.Fields(keyword), " ")
		if utf8.RuneCountInString(filter.Keyword) > 50 {
			writeSessionError(w, 422, "TICKET_INVALID_KEYWORD", "关键词长度不能超过 50 个字符")
			return
		}
		items, total, err := ticketRepo.Page(r.Context(), filter)
		if err != nil {
			slog.Error("读取工单列表失败", "err", err)
			writeSessionError(w, 500, "TICKET_INTERNAL", "读取工单列表失败")
			return
		}
		pages := (total + filter.Size - 1) / filter.Size
		var conditions []string
		var effectiveKeyword any
		if len(filter.Statuses) > 0 {
			conditions = append(conditions, "状态 "+strings.Join(filter.Statuses, "/"))
		}
		if filter.Keyword != "" {
			conditions = append(conditions, "关键词「"+filter.Keyword+"」")
			effectiveKeyword = filter.Keyword
		}
		scope := "共"
		if len(conditions) > 0 {
			scope = "按 " + strings.Join(conditions, " + ") + " 筛选到"
		}
		message := fmt.Sprintf("%s %d 个工单", scope, total)
		if pages > 1 {
			message += fmt.Sprintf("，当前第 %d/%d 页", filter.Page, pages)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"tickets": items, "total": total, "page": filter.Page, "page_size": filter.Size,
			"pages": pages, "has_more": filter.Page < pages, "order": filter.Direction,
			"statuses": filter.Statuses, "keyword": effectiveKeyword, "message": message,
		})
	}
}
