package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/tickets"
)

func publicTicketWrite(conn *sql.DB, operation string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		fields, ok := decodePublicTicketBody(w, r)
		if !ok {
			return
		}

		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}
		if operation == "status" && !user.IsAdmin {
			writeJSON(w, http.StatusForbidden, map[string]string{"error": "无权限", "error_code": "TICKET_FORBIDDEN"})
			return
		}

		ticketID, validID := ticketString(fields["ticket_id"])
		var reason string
		if operation != "status" {
			if !validID || ticketID == "" || utf8.RuneCountInString(ticketID) > 64 {
				writeTicketInputError(w, "工单号长度必须为 1 到 64 个字符")
				return
			}
			if raw, present := fields["reason"]; operation == "cancel" && present {
				var validReason bool
				reason, validReason = ticketString(raw)
				if !validReason || utf8.RuneCountInString(reason) > 500 {
					writeTicketInputError(w, "取消原因必须是长度不超过 500 的字符串")
					return
				}
				reason = strings.TrimSpace(reason)
			}
		}
		ownerID := user.ID
		if user.IsAdmin {
			ownerID = ""
		}
		var (
			result map[string]any
			err    error
			action string
		)
		switch operation {
		case "confirm":
			action = "确认"
			result, err = ticketWriter.ConfirmForOwner(r.Context(), ticketID, user.Name, ownerID)
		case "cancel":
			action = "取消"
			result, err = ticketWriter.CancelForOwner(r.Context(), ticketID, reason, user.Name, ownerID)
		case "status":
			status, _ := ticketString(fields["status"])
			result, err = ticketWriter.UpdateStatus(r.Context(), ticketID, status)
		}
		var ownership *tickets.OwnershipError
		if errors.As(err, &ownership) {
			writeJSON(w, http.StatusForbidden, map[string]any{
				"error": "无权限", "error_code": "TICKET_FORBIDDEN",
				"error_detail": map[string]string{"action": action, "ticket_id": ticketID, "owner_id": ownership.OwnerID},
			})
			return
		}
		if err != nil {
			writeTicketResult(w, ticketID, nil, err)
			return
		}
		response := map[string]any{"ticket": result}
		if operation != "status" {
			response["identity"] = user
		}
		writeJSON(w, http.StatusOK, response)
	}
}

func decodePublicTicketBody(w http.ResponseWriter, r *http.Request) (map[string]json.RawMessage, bool) {
	fields, ok := decodePublicObject(w, r)
	if !ok {
		writeTicketInputError(w, "请求体必须是 JSON 对象")
	}
	return fields, ok
}

func decodePublicObject(w http.ResponseWriter, r *http.Request) (map[string]json.RawMessage, bool) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	var fields map[string]json.RawMessage
	if err := decoder.Decode(&fields); err != nil || fields == nil || decoder.Decode(new(json.RawMessage)) != io.EOF {
		return nil, false
	}
	return fields, true
}

func ticketString(raw json.RawMessage) (string, bool) {
	var value *string
	if json.Unmarshal(raw, &value) != nil || value == nil {
		return "", false
	}
	return *value, true
}

func writeTicketInputError(w http.ResponseWriter, message string) {
	writeJSON(w, http.StatusUnprocessableEntity, map[string]string{
		"error": message, "error_code": string(tickets.CodeInvalidPayload),
	})
}
