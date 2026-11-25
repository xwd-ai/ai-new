package main

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"aibusiness/internal/auth"
	"github.com/golang-jwt/jwt/v5"
)

func TestPublicLogoutAndSessionCleanup(t *testing.T) {
	const secret = "test-revocation-signing-key"
	t.Setenv("AI_NEW_JWT_SECRET", secret)
	t.Setenv("AI_NEW_JWT_LEEWAY", "30")
	db, err := sql.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "revoke.db")+"?_txlock=immediate")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	now := time.Now().Unix()
	sign := func(owner, typ, id string, expiry int64) string {
		t.Helper()
		value, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
			"iss": auth.Issuer, "aud": auth.Audience, "sub": owner, "typ": typ,
			"iat": now - 3600, "exp": expiry, "jti": id,
		}).SignedString([]byte(secret))
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	access := sign("u1", auth.TokenTypeAccess, "access", now+120)
	refresh := sign("u1", auth.TokenTypeRefresh, "refresh", now+3600)
	foreign := sign("u2", auth.TokenTypeRefresh, "foreign", now+3600)
	expired := sign("u1", auth.TokenTypeAccess, "expired", now-120)
	leeway := sign("u1", auth.TokenTypeAccess, "leeway", now-5)
	_, err = db.Exec(`
		CREATE TABLE users (id TEXT, username TEXT, name TEXT, role TEXT);
		CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT, username TEXT, name TEXT, role TEXT, expires_at REAL);
		CREATE TABLE refresh_tokens (token TEXT PRIMARY KEY, user_id TEXT, expires_at REAL, revoked_at REAL);
		CREATE TABLE revoked_tokens (jti TEXT PRIMARY KEY, user_id TEXT, token_type TEXT, expires_at REAL, revoked_at REAL, reason TEXT);
		INSERT INTO users VALUES ('u1','customer','客户','customer'), ('u2','admin','管理员','admin');
		INSERT INTO sessions VALUES
			(?, 'u1', 'customer', '客户', 'customer', unixepoch()+120),
			('customer-session', 'u1', 'customer', '客户', 'customer', unixepoch()+120),
			('admin-session', 'u2', 'admin', '管理员', 'admin', unixepoch()+120),
			('expired-session', 'u1', 'customer', '客户', 'customer', unixepoch()-120);
		INSERT INTO refresh_tokens VALUES (?, 'u1', unixepoch()+3600, NULL),
			('expired-refresh', 'u1', unixepoch()-120, NULL);
	`, access, refresh)
	if err != nil {
		t.Fatal(err)
	}

	post := func(handler http.HandlerFunc, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/", strings.NewReader(body))
		if token != "" {
			r.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	logout := publicLogout(db)
	if w := post(logout, access, `{"refresh_token":"`+foreign+`"}`); w.Code != 401 {
		t.Fatalf("foreign refresh got %d", w.Code)
	}
	if w := post(publicSessionCleanup(db, db), "customer-session", ""); w.Code != 403 {
		t.Fatalf("customer cleanup got %d", w.Code)
	}
	for attempt := range 2 {
		w := post(logout, access, `{"refresh_token":"`+refresh+`"}`)
		var result auth.LogoutResult
		first := attempt == 0
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || w.Code != 200 ||
			result.RevokedAccess != first || result.RevokedRefresh != first || result.SessionRemoved != first {
			t.Fatalf("logout attempt %d: %d %s", attempt, w.Code, w.Body.String())
		}
	}
	if w := post(publicRefresh(db), refresh, ""); w.Code != 401 {
		t.Fatalf("revoked refresh accepted: %d", w.Code)
	}
	for _, raw := range []string{expired, leeway} {
		if w := post(logout, raw, ""); w.Code != 200 {
			t.Fatalf("expired access could not logout: %d %s", w.Code, w.Body.String())
		}
	}

	w := post(publicSessionCleanup(db, db), "admin-session", `{"limit":1}`)
	var cleanup auth.CleanupResult
	if err := json.Unmarshal(w.Body.Bytes(), &cleanup); err != nil || w.Code != 200 ||
		cleanup.Total != 3 || !cleanup.Truncated || cleanup.Deleted["revoked_tokens"] != 1 {
		t.Fatalf("unexpected cleanup response: %d %s", w.Code, w.Body.String())
	}
	r := httptest.NewRequest("GET", "/api/me", nil)
	r.Header.Set("Authorization", "Bearer "+leeway)
	w = httptest.NewRecorder()
	publicAccount(db).ServeHTTP(w, r)
	if w.Code != 200 || strings.TrimSpace(w.Body.String()) != `{"user":null}` {
		t.Fatalf("cleanup removed revocation before JWT leeway elapsed: %d %s", w.Code, w.Body.String())
	}
}
