package main

import (
	"database/sql"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"aibusiness/internal/auth"
	"github.com/golang-jwt/jwt/v5"
)

func TestPublicSessionRefresh(t *testing.T) {
	const secret = "test-shared-session-signing-key"
	t.Setenv("AI_NEW_JWT_SECRET", secret)
	t.Setenv("AI_NEW_JWT_TTL", "3600")
	db, err := sql.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "sessions.db")+"?_txlock=immediate")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	now := time.Now().Unix()
	sign := func(id, typ string) string {
		t.Helper()
		value, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
			"iss": auth.Issuer, "aud": auth.Audience, "sub": "u1", "typ": typ,
			"iat": now, "nbf": now, "exp": now + 7200, "jti": id,
			"role": "admin", "username": "stale",
		}).SignedString([]byte(secret))
		if err != nil {
			t.Fatal(err)
		}
		return value
	}

	refresh := sign("valid", "refresh")
	revoked := sign("revoked", "refresh")
	unknown := sign("unknown", "refresh")
	expiredRecord := sign("expired", "refresh")
	_, err = db.Exec(`
		CREATE TABLE users (id TEXT, username TEXT, name TEXT, role TEXT);
		CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, username TEXT, name TEXT, role TEXT, expires_at REAL);
		CREATE TABLE refresh_tokens (token TEXT PRIMARY KEY, user_id TEXT, expires_at REAL, revoked_at REAL);
		CREATE TABLE revoked_tokens (jti TEXT PRIMARY KEY);
		INSERT INTO users VALUES ('u1', 'current', '当前用户', 'customer');
		INSERT INTO sessions VALUES ('legacy', 'u1', 'current', '当前用户', 'customer', unixepoch()+60);
		INSERT INTO revoked_tokens VALUES ('revoked');
		INSERT INTO refresh_tokens VALUES (?, 'u1', unixepoch()+7200, NULL), (?, 'u1', unixepoch()-60, NULL);
	`, refresh, expiredRecord)
	if err != nil {
		t.Fatal(err)
	}

	for _, tc := range []struct {
		name, token, body, code string
		status                  int
	}{
		{"missing", "", "", "AUTH_TOKEN_MISSING", 401},
		{"invalid", "garbage", "", "AUTH_TOKEN_MALFORMED", 401},
		{"revoked", revoked, "", "AUTH_REFRESH_REVOKED", 401},
		{"unknown", unknown, "", "AUTH_REFRESH_UNKNOWN", 401},
		{"expired record", expiredRecord, "", "AUTH_TOKEN_EXPIRED", 401},
		{"access as refresh", sign("access", "access"), "", "AUTH_TOKEN_BAD_CLAIMS", 401},
		{"header", refresh, "", "", 200},
		{"body wins", revoked, `{"refresh_token":"` + refresh + `"}`, "", 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("POST", "/api/refresh", strings.NewReader(tc.body))
			if tc.token != "" {
				r.Header.Set("Authorization", "Bearer "+tc.token)
			}
			w := httptest.NewRecorder()
			publicRefresh(db).ServeHTTP(w, r)
			if w.Code != tc.status || w.Header().Get("Cache-Control") != "no-store" {
				t.Fatalf("status %d: %s", w.Code, w.Body.String())
			}
			if tc.status != 200 {
				var result map[string]string
				if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result["error_code"] != tc.code {
					t.Fatalf("unexpected error: %s", w.Body.String())
				}
				return
			}
			var result auth.RefreshResult
			if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result.RefreshToken != refresh ||
				result.RefreshExpiresAt != now+7200 || result.ExpiresIn != 3600 ||
				result.User.Username != "current" || result.User.Role != "customer" {
				t.Fatalf("unexpected refresh response: %s", w.Body.String())
			}
			claims, err := auth.VerifyAccessToken(result.Token)
			if err != nil || claims.Subject != "u1" || claims.ID == "valid" {
				t.Fatalf("invalid newly issued access token: %v", err)
			}
			var sessionOwner string
			if err := db.QueryRow("SELECT user_id FROM sessions WHERE token = ?", result.Token).Scan(&sessionOwner); err != nil || sessionOwner != "u1" {
				t.Fatalf("new access token not registered: %v", err)
			}
		})
	}

	for _, token := range []string{"legacy", sign("account", "access"), sign("revoked", "access"), ""} {
		r := httptest.NewRequest("GET", "/api/me", nil)
		r.Header.Set("Authorization", token)
		w := httptest.NewRecorder()
		publicAccount(db).ServeHTTP(w, r)
		want := `"username":"current"`
		if token == "" || token == sign("revoked", "access") {
			want = `"user":null`
		}
		if w.Code != 200 || !strings.Contains(w.Body.String(), want) {
			t.Fatalf("unexpected account response: %d %s", w.Code, w.Body.String())
		}
	}
}
