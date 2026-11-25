package main

import (
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"unicode/utf8"

	"aibusiness/internal/auth"
)

func publicRegister(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		fields, ok := decodePublicObject(w, r)
		username, validUser := ticketString(fields["username"])
		password, validPassword := ticketString(fields["password"])
		name, validName := "", true
		if raw, present := fields["name"]; present {
			name, validName = ticketString(raw)
		}
		if !ok || !validUser || !validPassword || !validName ||
			utf8.RuneCountInString(username) > 64 || utf8.RuneCountInString(password) > 128 ||
			utf8.RuneCountInString(name) > 32 {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "注册表单不合法"})
			return
		}
		user, err := auth.Register(r.Context(), conn, username, password, name)
		var weak *auth.WeakPasswordError
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, map[string]any{"user": user, "message": "注册成功"})
		case errors.Is(err, auth.ErrInvalidUsername):
			writeSessionError(w, http.StatusUnprocessableEntity, "AUTH_INVALID_USERNAME", err.Error())
		case errors.Is(err, auth.ErrUsernameTaken):
			writeSessionError(w, http.StatusConflict, "AUTH_USERNAME_TAKEN", err.Error())
		case errors.As(err, &weak):
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{
				"error": err.Error(), "error_code": "AUTH_WEAK_PASSWORD",
				"error_detail": map[string]any{"violations": weak.Violations},
			})
		default:
			slog.Error("创建账号失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "创建账号失败")
		}
	}
}
