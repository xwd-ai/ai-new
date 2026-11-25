package main

import (
	"database/sql"
	"log/slog"
	"net/http"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/tickets"
)

func publicTicketCreate(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		fields, ok := decodePublicTicketBody(w, r)
		if !ok {
			return
		}
		user, ok := requireTicketUser(conn, w, r)
		if !ok {
			return
		}

		description, valid := ticketString(fields["description"])
		if !valid || strings.TrimSpace(description) == "" || utf8.RuneCountInString(description) > 500 {
			writeTicketInputError(w, "问题描述必须是长度不超过 500 的非空字符串")
			return
		}

		input := tickets.CreateInput{UserID: user.ID, UserName: user.Name, Description: description}
		for field, target := range map[string]*string{"tried_steps": &input.TriedSteps, "evidence": &input.Evidence} {
			if raw, present := fields[field]; present {
				value, valid := ticketString(raw)
				if !valid || utf8.RuneCountInString(value) > 1000 {
					writeTicketInputError(w, field+" 必须是长度不超过 1000 的字符串")
					return
				}
				*target = strings.TrimSpace(value)
			}
		}

		ticket, created, err := ticketWriter.Create(r.Context(), input)
		if err != nil {
			slog.Error("建单失败", "err", err)
			writeJSON(w, http.StatusInternalServerError, map[string]string{
				"error": "工单操作失败", "error_code": "TICKET_INTERNAL",
			})
			return
		}
		message := "该问题已有未解决工单，已返回原工单"
		if created {
			message = "工单已生成待确认草稿，请确认后创建"
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"ticket": ticket, "created": created, "identity": user, "message": message,
		})
	}
}
