package auth

import (
	"aibusiness/internal/db"
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type Account struct {
	ID       string `json:"id"`
	Username string `json:"username"`
	Name     string `json:"name"`
	Role     string `json:"role"`
}

type RefreshResult struct {
	Token            string  `json:"token"`
	TokenType        string  `json:"token_type"`
	ExpiresIn        int64   `json:"expires_in"`
	ExpiresAt        int64   `json:"expires_at"`
	RefreshToken     string  `json:"refresh_token"`
	RefreshExpiresIn int64   `json:"refresh_expires_in"`
	RefreshExpiresAt int64   `json:"refresh_expires_at"`
	User             Account `json:"user"`
}

var (
	ErrRefreshUnknown  = errors.New("续期令牌未登记，请重新登录")
	ErrRefreshRevoked  = errors.New("续期令牌已失效，请重新登录")
	ErrRefreshUserGone = errors.New("账号不存在，请重新登录")
	ErrSecretMissing   = errors.New("JWT 签名密钥未配置")
)

func RefreshAccess(ctx context.Context, conn *sql.DB, raw string) (RefreshResult, error) {
	var result RefreshResult
	if !HasConfiguredSecret() {
		return result, ErrSecretMissing
	}
	claims, err := VerifyRefreshToken(raw)
	if err != nil {
		return result, err
	}
	// The SQLite write transaction serializes the revocation checks and new session insert.
	tx, err := db.BeginWrite(ctx, conn)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()

	var owner string
	var expires float64
	var revokedAt sql.NullFloat64
	err = tx.QueryRowContext(ctx, db.LockRows(conn, `
		SELECT user_id, expires_at, revoked_at FROM refresh_tokens WHERE token = ?`),
		raw).Scan(&owner, &expires, &revokedAt)
	var revoked bool
	if err := tx.QueryRowContext(ctx, `
		SELECT EXISTS (SELECT 1 FROM revoked_tokens WHERE jti = ? AND jti <> '')`,
		claims.ID).Scan(&revoked); err != nil {
		return result, err
	}
	if revoked {
		return result, ErrRefreshRevoked
	}

	switch {
	case errors.Is(err, sql.ErrNoRows):
		return result, ErrRefreshUnknown
	case err != nil:
		return result, err
	case owner != claims.Subject:
		return result, ErrRefreshUnknown
	case revokedAt.Valid:
		return result, ErrRefreshRevoked
	}

	now := time.Now()
	if expires < float64(now.UnixNano())/1e9 || !now.Before(claims.ExpiresAt.Add(ClockLeeway())) {
		return result, jwt.ErrTokenExpired
	}
	err = tx.QueryRowContext(ctx, "SELECT id, username, name, role FROM users WHERE id = ?", owner).
		Scan(&result.User.ID, &result.User.Username, &result.User.Name, &result.User.Role)
	if errors.Is(err, sql.ErrNoRows) {
		return result, ErrRefreshUserGone
	}
	if err != nil {
		return result, err
	}
	issued, ttl := now.Unix(), accessTTL()
	token, err := issueToken(result.User, TokenTypeAccess, issued, ttl)
	if err != nil {
		return result, err
	}
	expiresAt := issued + ttl
	if err := recordSession(ctx, tx, result.User, token, expiresAt); err != nil {
		return result, err
	}
	if err := tx.Commit(); err != nil {
		return result, err
	}

	result.Token = token
	result.TokenType = "Bearer"
	result.ExpiresIn = ttl
	result.ExpiresAt = expiresAt
	result.RefreshToken = raw
	result.RefreshExpiresAt = claims.ExpiresAt.Unix()
	result.RefreshExpiresIn = max(0, result.RefreshExpiresAt-issued)
	return result, nil
}

func issueToken(account Account, typ string, issued, ttl int64) (string, error) {
	var randomID [16]byte
	rand.Read(randomID[:])
	return jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"iss": Issuer, "aud": Audience, "sub": account.ID, "username": account.Username,
		"role": normalizedRole(account.Role), "typ": typ, "iat": issued, "nbf": issued,
		"exp": issued + ttl, "jti": hex.EncodeToString(randomID[:]),
	}).SignedString(secret())
}

func recordSession(ctx context.Context, tx *sql.Tx, account Account, token string, expiresAt int64) error {
	_, err := tx.ExecContext(ctx, `
		INSERT INTO sessions (token, user_id, username, name, role, expires_at)
		VALUES (?, ?, ?, ?, ?, ?)`,
		token, account.ID, account.Username, account.Name, normalizedRole(account.Role), expiresAt)
	return err
}

func normalizedRole(role string) string {
	if strings.EqualFold(strings.TrimSpace(role), "admin") {
		return "admin"
	}
	return "customer"
}

func accessTTL() int64 {
	return tokenTTL("AI_NEW_JWT_TTL", 30*60, 60, 7*24*3600)
}

func refreshTTL() int64 {
	return tokenTTL("AI_NEW_JWT_REFRESH_TTL", 30*24*3600, 3600, 180*24*3600)
}

func tokenTTL(name string, fallback, minimum, maximum int64) int64 {
	ttl, err := strconv.ParseInt(strings.TrimSpace(os.Getenv(name)), 10, 64)
	if err != nil || ttl < minimum || ttl > maximum {
		return fallback
	}
	return ttl
}
