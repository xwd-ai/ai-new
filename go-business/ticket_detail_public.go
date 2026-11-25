package main

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/tickets"
)

func publicTicketDetail(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		id := r.PathValue("ticket_id")
		owner := user.ID
		if user.IsAdmin {
			owner = ""
		}
		result, message, err := ticketRepo.Detail(r.Context(), id, owner)
		if err != nil {
			writeTicketLookupError(w, id, "查看", err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ticket": result, "message": message})
	}
}

func publicTicketTransferDecision(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		q := r.URL.Query()
		input := tickets.TransferInput{ID: r.PathValue("ticket_id"), OwnerID: user.ID, UserText: q.Get("user_text"), Feature: q.Get("feature")}
		if user.IsAdmin {
			input.OwnerID = ""
		}
		if utf8.RuneCountInString(input.UserText) > 500 || utf8.RuneCountInString(input.Feature) > 50 {
			writeTicketInputError(w, "转人工判定参数超长")
			return
		}
		result, err := ticketRepo.TransferDecision(r.Context(), repo, input)
		if err != nil {
			writeTicketLookupError(w, input.ID, "判定", err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"decision": result, "message": result.Message})
	}
}

func publicTicketTransfer(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		user, ok := requireTicketAdmin(conn, w, r)
		if !ok {
			return
		}
		fields, ok := decodePublicTicketBody(w, r)
		if !ok {
			return
		}
		id, valid := ticketString(fields["ticket_id"])
		if !valid || id == "" || utf8.RuneCountInString(id) > 64 {
			writeTicketInputError(w, "工单号不合法")
			return
		}
		input := tickets.TransferInput{ID: id, Operator: user.Name}
		for _, field := range []struct {
			name  string
			value *string
			limit int
		}{
			{"reason", &input.Reason, 500}, {"user_text", &input.UserText, 500}, {"feature", &input.Feature, 50},
		} {
			if raw, present := fields[field.name]; present {
				value, valid := ticketString(raw)
				if !valid || utf8.RuneCountInString(value) > field.limit {
					writeTicketInputError(w, "转人工请求参数不合法")
					return
				}
				*field.value = strings.Join(strings.Fields(value), " ")
			}
		}
		if raw, present := fields["auto"]; present {
			value := strings.Trim(string(raw), "\"")
			input.Auto, ok = pendingBool(value, true)
			if !ok {
				writeTicketInputError(w, "auto 必须为布尔值")
				return
			}
		}
		result, decision, err := ticketWriter.Transfer(r.Context(), repo, input)
		if errors.Is(err, tickets.ErrTransferRuleNotMatched) {
			writeJSON(w, http.StatusConflict, map[string]any{
				"error": "未命中转人工规则：" + decision.Message, "error_code": "TICKET_TRANSFER_RULE_NOT_MATCHED",
				"error_detail": map[string]any{"decision": decision},
			})
			return
		}
		if err != nil {
			writeTicketLookupError(w, id, "转人工", err)
			return
		}
		transfer := result["transfer"].(map[string]any)
		message := fmt.Sprintf("工单 %s 已转人工", id)
		if len(decision.ReasonCodes) > 0 {
			message += "（命中规则：" + strings.Join(decision.ReasonCodes, "/") + "）"
		}
		message += "，原因：" + transfer["reason"].(string)
		writeJSON(w, http.StatusOK, map[string]any{"ticket": result, "decision": decision, "message": message})
	}
}

func writeTicketLookupError(w http.ResponseWriter, id, action string, err error) {
	var ownership *tickets.OwnershipError
	if errors.As(err, &ownership) {
		writeJSON(w, http.StatusForbidden, map[string]any{
			"error": "无权限", "error_code": "TICKET_FORBIDDEN",
			"error_detail": map[string]string{"action": action, "ticket_id": id, "owner_id": ownership.OwnerID},
		})
		return
	}
	if errors.Is(err, sql.ErrNoRows) {
		err = tickets.ErrNotFound
	}
	writeTicketResult(w, id, nil, err)
}
