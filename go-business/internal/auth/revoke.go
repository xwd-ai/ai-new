package auth

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

var ErrForeignRefresh = errors.New("续期令牌不属于当前登录用户")

type LogoutResult struct {
	Message          string `json:"message"`
	User             any    `json:"user"`
	RevokedAccess    bool   `json:"revoked_access"`
	AccessExpiresAt  int64  `json:"access_expires_at"`
	SessionRemoved   bool   `json:"session_removed"`
	RevokedRefresh   bool   `json:"revoked_refresh"`
	RefreshExpiresAt *int64 `json:"refresh_expires_at"`
}

func Logout(ctx context.Context, conn *sql.DB, accessToken, refreshToken string) (LogoutResult, error) {
	var result LogoutResult
	if !HasConfiguredSecret() {
		return result, ErrSecretMissing
	}
	access, err := VerifyAccessToken(accessToken)
	if errors.Is(err, jwt.ErrTokenExpired) &&
		!errors.Is(err, jwt.ErrTokenInvalidIssuer) && !errors.Is(err, jwt.ErrTokenInvalidAudience) &&
		!errors.Is(err, jwt.ErrTokenNotValidYet) {
		access, err = verifyToken(accessToken, TokenTypeAccess, false)
	} else if err == nil {
		err = ValidateIdentityClaims(access)
	}
	if err != nil {
		return result, err
	}

	var refresh *Claims
	if refreshToken != "" {
		refresh, err = verifyToken(refreshToken, TokenTypeRefresh, false)
		if err != nil {
			return result, err
		}
		if refresh.Subject != access.Subject {
			return result, ErrForeignRefresh
		}
	}

	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()
	now := float64(time.Now().UnixNano()) / 1e9
	result.RevokedAccess, err = revokeToken(ctx, tx, access, now)
	if err != nil {
		return result, err
	}
	deletion, err := tx.ExecContext(ctx, "DELETE FROM sessions WHERE token = ?", accessToken)
	if err != nil {
		return result, err
	}
	removed, err := deletion.RowsAffected()
	if err != nil {
		return result, err
	}
	if refresh != nil {
		result.RevokedRefresh, err = revokeToken(ctx, tx, refresh, now)
		if err != nil {
			return result, err
		}
		if _, err := tx.ExecContext(ctx, `
			UPDATE refresh_tokens SET revoked_at = ?
			WHERE token = ? AND user_id = ? AND revoked_at IS NULL`,
			now, refreshToken, refresh.Subject); err != nil {
			return result, err
		}
		expiry := refresh.ExpiresAt.Unix()
		result.RefreshExpiresAt = &expiry
	}
	var user Account
	err = tx.QueryRowContext(ctx, "SELECT id, username, name, role FROM users WHERE id = ?", access.Subject).
		Scan(&user.ID, &user.Username, &user.Name, &user.Role)
	switch {
	case errors.Is(err, sql.ErrNoRows):
		result.User = map[string]any{}
	case err != nil:
		return result, err
	default:
		result.User = user
	}
	if err := tx.Commit(); err != nil {
		return result, err
	}
	result.Message = "已退出登录"
	result.AccessExpiresAt = access.ExpiresAt.Unix()
	result.SessionRemoved = removed != 0
	return result, nil
}

func revokeToken(ctx context.Context, tx *sql.Tx, claims *Claims, now float64) (bool, error) {
	if claims.ID == "" {
		return false, nil
	}
	result, err := tx.ExecContext(ctx, `
		INSERT INTO revoked_tokens (jti, user_id, token_type, expires_at, revoked_at, reason)
		VALUES (?, ?, ?, ?, ?, 'logout') ON CONFLICT (jti) DO NOTHING`,
		claims.ID, claims.Subject, claims.Typ, claims.ExpiresAt.Unix(), now)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count != 0, err
}

type CleanupResult struct {
	Message   string           `json:"message"`
	Deleted   map[string]int64 `json:"deleted"`
	Total     int64            `json:"total"`
	Cutoff    int64            `json:"cutoff"`
	Retention int              `json:"retention"`
	Truncated bool             `json:"truncated"`
}

func CleanupExpired(ctx context.Context, conn *sql.DB, retention, limit int) (CleanupResult, error) {
	now := float64(time.Now().UnixNano()) / 1e9
	cutoff := now - float64(retention)
	result := CleanupResult{Deleted: make(map[string]int64, 3), Cutoff: int64(cutoff), Retention: retention}
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()

	for _, table := range []struct{ name, key string }{
		{"sessions", "token"}, {"refresh_tokens", "token"}, {"revoked_tokens", "jti"},
	} {
		expiresBefore := cutoff
		if table.name == "revoked_tokens" {
			expiresBefore = min(cutoff, now-ClockLeeway().Seconds())
		}
		query := fmt.Sprintf(`DELETE FROM %[1]s WHERE %[2]s IN (
			SELECT %[2]s FROM %[1]s WHERE expires_at < ? ORDER BY expires_at, %[2]s LIMIT ?)`,
			table.name, table.key)
		deleted, err := tx.ExecContext(ctx, query, expiresBefore, limit)
		if err != nil {
			return result, err
		}
		count, err := deleted.RowsAffected()
		if err != nil {
			return result, err
		}
		result.Deleted[table.name] = count
		result.Total += count
		result.Truncated = result.Truncated || count >= int64(limit)
	}
	if err := tx.Commit(); err != nil {
		return result, err
	}
	result.Message = fmt.Sprintf("已清理 %d 条过期登录记录", result.Total)
	return result, nil
}
