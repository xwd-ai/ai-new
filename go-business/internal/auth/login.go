package auth

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"database/sql"
	"encoding/hex"
	"errors"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"
)

var ErrInvalidCredentials = errors.New("用户名或密码不正确")

const placeholderHash = "$2b$12$sEi4W.uy8dqAnA/yZybMn.5v344bf3eEqrCiF6wyb0p2AWAQFWIIu"

const loginAccount = "SELECT id, username, name, role, password_hash FROM users"

// Login verifies the password and registers both credentials atomically.
func Login(ctx context.Context, conn *sql.DB, username, password string) (RefreshResult, error) {
	var result RefreshResult
	if !HasConfiguredSecret() {
		return result, ErrSecretMissing
	}
	if username == "" || password == "" {
		return result, ErrInvalidCredentials
	}
	var account Account
	var stored string
	row := conn.QueryRowContext(ctx, loginAccount+" WHERE username = ?", username)
	err := row.Scan(&account.ID, &account.Username, &account.Name, &account.Role, &stored)
	if errors.Is(err, sql.ErrNoRows) {
		_, _ = verifyPassword(password, placeholderHash)
		return result, ErrInvalidCredentials
	}
	if err != nil {
		return result, err
	}
	valid, currentCost := verifyPassword(password, stored)
	if !valid {
		return result, ErrInvalidCredentials
	}

	var upgraded []byte
	cost := bcryptCost()
	if currentCost < cost && len(password) <= 72 {
		if hash, err := bcrypt.GenerateFromPassword([]byte(password), cost); err == nil {
			upgraded = hash
		}
	}

	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()

	var currentHash string
	err = tx.QueryRowContext(ctx, loginAccount+" WHERE id = ? AND username = ?", account.ID, username).
		Scan(&account.ID, &account.Username, &account.Name, &account.Role, &currentHash)
	if errors.Is(err, sql.ErrNoRows) {
		return result, ErrInvalidCredentials
	}
	if err != nil {
		return result, err
	}
	if currentHash != stored {
		// A concurrent login may have upgraded the hash, or the password may have changed.
		if valid, _ := verifyPassword(password, currentHash); !valid {
			return result, ErrInvalidCredentials
		}
		upgraded = nil
	}

	now, access, refresh := time.Now().Unix(), accessTTL(), refreshTTL()
	accessToken, err := issueToken(account, TokenTypeAccess, now, access)
	if err != nil {
		return result, err
	}
	refreshToken, err := issueToken(account, TokenTypeRefresh, now, refresh)
	if err != nil {
		return result, err
	}
	if err := recordSession(ctx, tx, account, accessToken, now+access); err != nil {
		return result, err
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO refresh_tokens (token, user_id, expires_at) VALUES (?, ?, ?)`,
		refreshToken, account.ID, now+refresh); err != nil {
		return result, err
	}
	if err := tx.Commit(); err != nil {
		return result, err
	}

	// Password upgrade is best effort and must not overwrite a concurrent password change.
	if upgraded != nil {
		if _, err := conn.ExecContext(ctx, "UPDATE users SET password_hash = ? WHERE id = ? AND password_hash = ?",
			string(upgraded), account.ID, stored); err != nil {
			slog.Warn("密码哈希升级写入失败")
		}
	}

	return RefreshResult{
		Token: accessToken, TokenType: "Bearer", ExpiresIn: access, ExpiresAt: now + access,
		RefreshToken: refreshToken, RefreshExpiresIn: refresh, RefreshExpiresAt: now + refresh, User: account,
	}, nil
}

func verifyPassword(password, stored string) (bool, int) {
	if len(stored) == 60 && (strings.HasPrefix(stored, "$2a$") || strings.HasPrefix(stored, "$2b$") || strings.HasPrefix(stored, "$2y$")) {
		if len(password) > 72 {
			return false, 0
		}
		cost, err := bcrypt.Cost([]byte(stored))
		return err == nil && bcrypt.CompareHashAndPassword([]byte(stored), []byte(password)) == nil, cost
	}
	digest := sha256.Sum256([]byte(password))
	return subtle.ConstantTimeCompare([]byte(hex.EncodeToString(digest[:])), []byte(stored)) == 1, 0
}

func bcryptCost() int {
	cost, err := strconv.Atoi(strings.TrimSpace(os.Getenv("BCRYPT_COST")))
	if err != nil || cost < 0 {
		return 12
	}
	return max(4, min(16, cost))
}
