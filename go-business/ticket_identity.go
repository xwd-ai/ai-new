package main

import (
	"database/sql"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"aibusiness/internal/auth"
	"aibusiness/internal/tickets"
)

type ticketUser struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Role    string `json:"role"`
	IsAdmin bool   `json:"is_admin"`
}

func requireTicketUser(conn *sql.DB, w http.ResponseWriter, r *http.Request) (ticketUser, bool) {
	user, err := resolveTicketUser(conn, r)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "未登录", "error_code": "TICKET_UNAUTHORIZED"})
	case err != nil:
		slog.Error("读取工单请求身份失败", "err", err)
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "读取登录状态失败"})
	}
	return user, err == nil
}

func resolveAccount(conn *sql.DB, r *http.Request) (auth.Account, error) {
	var user auth.Account
	var err error
	token := strings.TrimSpace(r.Header.Get("Authorization"))
	if fields := strings.Fields(token); len(fields) > 1 && strings.EqualFold(fields[0], "Bearer") {
		token = strings.TrimSpace(token[len(fields[0]):])
	}
	if token == "" {
		return user, sql.ErrNoRows
	}
	ctx := r.Context()
	if strings.Count(token, ".") == 2 {
		if !auth.HasConfiguredSecret() {
			return user, errors.New("公开接口需要配置 JWT 签名密钥")
		}
		claims, verifyErr := auth.VerifyAccessToken(token)
		if verifyErr != nil || auth.ValidateIdentityClaims(claims) != nil {
			return user, sql.ErrNoRows
		}
		err = conn.QueryRowContext(ctx, `
			SELECT id, role, name, username FROM users WHERE id = ? AND id <> ''
			AND NOT EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ? AND jti <> '')`,
			claims.Subject, claims.ID).Scan(&user.ID, &user.Role, &user.Name, &user.Username)
	} else {
		err = conn.QueryRowContext(ctx, `
		SELECT u.id, u.role, u.name, u.username FROM sessions s JOIN users u
		ON u.id = s.user_id OR u.username = s.username
		WHERE s.token = ? AND s.expires_at >= ? AND u.id <> ''
		ORDER BY CASE WHEN u.id = s.user_id THEN 0 ELSE 1 END LIMIT 1`,
			token, float64(time.Now().UnixNano())/1e9).Scan(&user.ID, &user.Role, &user.Name, &user.Username)
	}
	if err != nil {
		return user, err
	}
	return user, nil
}

func resolveTicketUser(conn *sql.DB, r *http.Request) (ticketUser, error) {
	account, err := resolveAccount(conn, r)
	if err != nil {
		return ticketUser{}, err
	}
	user := ticketUser{ID: account.ID, Name: account.Name, Role: account.Role}
	user.IsAdmin = strings.EqualFold(strings.TrimSpace(user.Role), "admin")
	user.Role = "customer"
	if user.IsAdmin {
		user.Role = "admin"
	}
	if user.Name == "" {
		user.Name = account.Username
	}
	if strings.TrimSpace(user.Name) == "" {
		user.Name = user.ID
	} else {
		user.Name = tickets.CleanOperator(user.Name)
	}
	return user, nil
}
