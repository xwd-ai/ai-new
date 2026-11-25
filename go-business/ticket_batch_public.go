package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"unicode/utf8"
)

func publicTicketBatch(conn *sql.DB, transfer bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketAdmin(conn, w, r)
		if !ok {
			return
		}
		fields, ok := decodePublicTicketBody(w, r)
		if !ok {
			return
		}
		var rawIDs []json.RawMessage
		if json.Unmarshal(fields["ticket_ids"], &rawIDs) != nil || len(rawIDs) == 0 || len(rawIDs) > 50 {
			writeTicketInputError(w, "每批必须提供 1 到 50 个工单号")
			return
		}
		ids := make([]string, 0, len(rawIDs))
		for _, raw := range rawIDs {
			id, valid := ticketString(raw)
			if !valid {
				writeTicketInputError(w, "工单号必须为字符串")
				return
			}
			id = strings.TrimSpace(id)
			if id != "" && !slices.Contains(ids, id) {
				ids = append(ids, id)
			}
		}
		if len(ids) == 0 {
			writeTicketInputError(w, "工单号列表不能为空")
			return
		}
		var reason string
		if raw, present := fields["reason"]; present {
			value, valid := ticketString(raw)
			if !valid || utf8.RuneCountInString(value) > 500 {
				writeTicketInputError(w, "批量操作原因不能超过 500 个字符")
				return
			}
			reason = strings.Join(strings.Fields(value), " ")
		}
		result, err := ticketWriter.BatchTransition(r.Context(), ids, transfer, reason, user.Name)
		if err != nil {
			slog.Error("批量更新工单失败", "err", err)
			writeSessionError(w, 500, "TICKET_INTERNAL", "批量更新工单失败")
			return
		}
		action := "关闭"
		if transfer {
			action = "转人工"
		}
		message := fmt.Sprintf("批量%s完成：成功 %d 个，失败 %d 个", action, result.Succeeded, result.Failed)
		if result.Failed > 0 {
			first := result.Failures[0]
			message += fmt.Sprintf("（%s：%s）", first.TicketID, first.Reason)
		}
		writeJSON(w, 200, map[string]any{"result": result, "message": message})
	}
}
