package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"aibusiness/internal/auth"
	"golang.org/x/crypto/bcrypt"
)

func TestPublicLoginMigratesLegacyPasswordAndRegistersTokens(t *testing.T) {
	t.Setenv("AI_NEW_JWT_SECRET", "test-go-login-signing-key")
	t.Setenv("BCRYPT_COST", "4")
	t.Setenv("AI_NEW_JWT_REFRESH_TTL", "7200")
	db, err := sql.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "login.db")+"?_txlock=immediate")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	const password = "LegacyPassword123"
	digest := fmt.Sprintf("%x", sha256.Sum256([]byte(password)))
	_, err = db.Exec(`
		CREATE TABLE users (id TEXT, username TEXT, name TEXT, role TEXT, password_hash TEXT);
		CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, username TEXT, name TEXT, role TEXT, expires_at REAL);
		CREATE TABLE refresh_tokens (token TEXT PRIMARY KEY, user_id TEXT, expires_at REAL, revoked_at REAL);
		INSERT INTO users VALUES ('u1', 'customer', '客户', 'customer', ?);
	`, digest)
	if err != nil {
		t.Fatal(err)
	}
	login := func(body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/api/login", strings.NewReader(body))
		w := httptest.NewRecorder()
		publicLogin(db).ServeHTTP(w, r)
		return w
	}
	if w := login(`{"username":"customer","password":"wrong"}`); w.Code != 200 ||
		strings.TrimSpace(w.Body.String()) != `{"token":"","user":{}}` {
		t.Fatalf("unexpected bad-credentials response: %d %s", w.Code, w.Body.String())
	}

	if _, err := db.Exec(`CREATE TRIGGER fail_refresh BEFORE INSERT ON refresh_tokens
		BEGIN SELECT RAISE(ABORT, 'refresh insert failed'); END`); err != nil {
		t.Fatal(err)
	}
	loginBody := `{"username":"customer","password":"` + password + `"}`
	if w := login(loginBody); w.Code != 500 {
		t.Fatalf("expected issuance failure, got %d", w.Code)
	}
	var sessionCount int
	if err := db.QueryRow("SELECT COUNT(*) FROM sessions").Scan(&sessionCount); err != nil || sessionCount != 0 {
		t.Fatalf("partially registered credentials: sessions=%d, err=%v", sessionCount, err)
	}
	if _, err := db.Exec("DROP TRIGGER fail_refresh"); err != nil {
		t.Fatal(err)
	}

	for range 2 {
		w := login(loginBody)
		var result auth.RefreshResult
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || w.Code != 200 ||
			result.User.ID != "u1" || result.RefreshExpiresIn != 7200 {
			t.Fatalf("unexpected login result: status=%d, err=%v", w.Code, err)
		}
		if _, err := auth.VerifyAccessToken(result.Token); err != nil {
			t.Fatal("Go access token could not be verified")
		}
		if _, err := auth.VerifyRefreshToken(result.RefreshToken); err != nil {
			t.Fatal("Go refresh token could not be verified")
		}
		var owner string
		if err := db.QueryRow("SELECT user_id FROM refresh_tokens WHERE token = ?", result.RefreshToken).Scan(&owner); err != nil || owner != "u1" {
			t.Fatal("refresh token was not registered to the account")
		}
	}
	var hash string
	if err := db.QueryRow("SELECT password_hash FROM users WHERE id = 'u1'").Scan(&hash); err != nil ||
		bcrypt.CompareHashAndPassword([]byte(hash), []byte(password)) != nil {
		t.Fatal("legacy password was not upgraded")
	}
}
