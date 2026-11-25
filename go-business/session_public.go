package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"unicode/utf8"

	"aibusiness/internal/auth"
	"github.com/golang-jwt/jwt/v5"
)

func publicLogin(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		fields, ok := decodePublicObject(w, r)
		username, validUsername := ticketString(fields["username"])
		password, validPassword := ticketString(fields["password"])
		if !ok || !validUsername || !validPassword {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "username 和 password 必须是字符串"})
			return
		}
		result, err := auth.Login(r.Context(), conn, username, password)
		switch {
		case errors.Is(err, auth.ErrInvalidCredentials):
			writeJSON(w, http.StatusOK, map[string]any{"token": "", "user": map[string]any{}})
		case err != nil:
			slog.Error("登录凭证签发失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_TOKEN_SIGNING_FAILED", "登录凭证签发失败")
		default:
			writeJSON(w, http.StatusOK, result)
		}
	}
}

func publicAccount(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		account, err := resolveAccount(conn, r)
		switch {
		case errors.Is(err, sql.ErrNoRows):
			writeJSON(w, http.StatusOK, map[string]any{"user": nil})
		case err != nil:
			slog.Error("读取当前账号失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "读取登录状态失败")
		default:
			writeJSON(w, http.StatusOK, map[string]any{"user": account})
		}
	}
}

func publicRefresh(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		token, ok := readRefreshToken(w, r)
		if !ok {
			return
		}
		if token == "" {
			token = refreshHeaderToken(r.Header.Get("Authorization"))
		}
		token = strings.TrimSpace(token)
		if token == "" {
			writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MISSING", "缺少续期令牌")
			return
		}
		if len(token) > 4096 {
			writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MALFORMED", "令牌长度超出上限")
			return
		}

		result, err := auth.RefreshAccess(r.Context(), conn, token)
		switch {
		case err == nil:
			writeJSON(w, http.StatusOK, result)
		case errors.Is(err, auth.ErrRefreshUnknown):
			writeSessionError(w, http.StatusUnauthorized, "AUTH_REFRESH_UNKNOWN", err.Error())
		case errors.Is(err, auth.ErrRefreshRevoked):
			writeSessionError(w, http.StatusUnauthorized, "AUTH_REFRESH_REVOKED", err.Error())
		case errors.Is(err, auth.ErrRefreshUserGone):
			writeSessionError(w, http.StatusUnauthorized, "AUTH_REFRESH_USER_GONE", err.Error())
		default:
			if writeTokenError(w, err) {
				return
			}
			slog.Error("换发访问令牌失败", "err", err)
			writeSessionError(w, http.StatusInternalServerError, "AUTH_ERROR", "换发访问令牌失败")
		}
	}
}

func readSessionObject(w http.ResponseWriter, r *http.Request) (map[string]json.RawMessage, bool) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16))
	var body map[string]json.RawMessage
	err := decoder.Decode(&body)
	if err != io.EOF && (err != nil || decoder.Decode(new(json.RawMessage)) != io.EOF) {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "请求体必须是 JSON 对象"})
		return nil, false
	}
	return body, true
}

func readRefreshToken(w http.ResponseWriter, r *http.Request) (string, bool) {
	body, ok := readSessionObject(w, r)
	if !ok {
		return "", false
	}
	raw, present := body["refresh_token"]
	if !present {
		return "", true
	}
	token, valid := ticketString(raw)
	if !valid || utf8.RuneCountInString(token) > 4096 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "续期令牌必须是长度不超过 4096 的字符串"})
		return "", false
	}
	return token, true
}

func writeTokenError(w http.ResponseWriter, err error) bool {
	switch {
	case errors.Is(err, jwt.ErrTokenExpired):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_EXPIRED", "令牌已过期")
	case errors.Is(err, jwt.ErrTokenNotValidYet), errors.Is(err, jwt.ErrTokenUsedBeforeIssued):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_NOT_YET_VALID", "令牌尚未生效")
	case errors.Is(err, jwt.ErrTokenMalformed):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_MALFORMED", "令牌结构无效")
	case errors.Is(err, jwt.ErrTokenUnverifiable):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_UNSUPPORTED_ALG", "令牌签名算法不受支持")
	case errors.Is(err, jwt.ErrTokenSignatureInvalid):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_BAD_SIGNATURE", "令牌签名校验失败")
	case errors.Is(err, auth.ErrWrongType), errors.Is(err, jwt.ErrTokenInvalidClaims):
		writeSessionError(w, http.StatusUnauthorized, "AUTH_TOKEN_BAD_CLAIMS", "令牌声明无效")
	default:
		return false
	}
	return true
}

func refreshHeaderToken(value string) string {
	value = strings.TrimSpace(value)
	fields := strings.Fields(value)
	if len(fields) > 0 && strings.EqualFold(fields[0], "Bearer") {
		return strings.TrimSpace(value[len(fields[0]):])
	}
	return value
}

func writeSessionError(w http.ResponseWriter, status int, code, message string) {
	writeJSON(w, status, map[string]string{"error": message, "error_code": code})
}
