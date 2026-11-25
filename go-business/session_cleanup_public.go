package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"aibusiness/internal/auth"
)

func publicLogout(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		refresh, ok := readRefreshToken(w, r)
		if !ok {
			return
		}
		access := refreshHeaderToken(r.Header.Get("Authorization"))
		if access == "" {
			writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MISSING", "缺少认证令牌")
			return
		}
		if len(access) > 4096 {
			writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MALFORMED", "令牌长度超出上限")
			return
		}
		result, err := auth.Logout(r.Context(), conn, access, strings.TrimSpace(refresh))
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, result)
		case errors.Is(err, auth.ErrForeignRefresh):
			writeJSON(w, http.StatusUnauthorized, map[string]any{
				"error": err.Error(), "error_code": "AUTH_TOKEN_BAD_CLAIMS",
				"error_detail": map[string]string{"claim": "sub"},
			})
		default:
			if !writeTokenError(w, err) {
				slog.Error("登出失败", "err", err)
				writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "登出失败")
			}
		}
	}
}

func publicSessionCleanup(read, write *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		fields, ok := readSessionObject(w, r)
		if !ok {
			return
		}
		account, err := resolveAccount(read, r)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MISSING", "未登录")
			return
		case err != nil:
			slog.Error("读取清理请求身份失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "读取登录状态失败")
			return
		case !strings.EqualFold(strings.TrimSpace(account.Role), "admin"):
			writeJSON(w, http.StatusForbidden, map[string]any{
				"error": "当前账号无权执行该操作", "error_code": "AUTH_FORBIDDEN",
				"error_detail": map[string]any{
					"required_roles": []string{"admin"}, "role": "customer", "action": "执行该操作",
				},
			})
			return
		}

		retention, limit := 0, 2000
		if !readCleanupInt(fields["retention"], &retention, 0, 30*24*3600) ||
			!readCleanupInt(fields["limit"], &limit, 1, 10000) {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "清理参数超出允许范围"})
			return
		}
		result, err := auth.CleanupExpired(r.Context(), write, retention, limit)
		if err != nil {
			slog.Error("清理过期登录凭证失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "清理过期登录凭证失败")
			return
		}
		writeJSON(w, http.StatusOK, result)
	}
}

func readCleanupInt(raw json.RawMessage, dest *int, minimum, maximum int) bool {
	if raw == nil {
		return true
	}
	var value *int
	if json.Unmarshal(raw, &value) != nil || value == nil || *value < minimum || *value > maximum {
		return false
	}
	*dest = *value
	return true
}
