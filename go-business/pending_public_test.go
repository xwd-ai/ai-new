package main

import (
	"database/sql"
	"encoding/json"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"aibusiness/internal/tickets"
	"github.com/golang-jwt/jwt/v5"
)

func TestPublicTicketAuthorization(t *testing.T) {
	const secret = "pending-test-signing-key"
	t.Setenv("AI_NEW_JWT_SECRET", secret)
	conn, err := sql.Open("sqlite", filepath.Join(t.TempDir(), "pending.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, err = conn.Exec(`
		CREATE TABLE users (id TEXT, username TEXT, role TEXT, name TEXT);
		CREATE TABLE sessions (token TEXT, user_id TEXT, username TEXT, expires_at REAL);
		CREATE TABLE revoked_tokens (jti TEXT);
		CREATE TABLE ticket_number_counter (id INTEGER PRIMARY KEY, last_number BIGINT NOT NULL);
		CREATE TABLE tried_steps (user_id TEXT, step TEXT, at REAL);
		CREATE TABLE tickets (id TEXT, dedup_key TEXT UNIQUE, user_id TEXT, user_name TEXT,
			description TEXT, tried_steps TEXT, evidence TEXT, status TEXT, created_at REAL, replies TEXT);
		CREATE TABLE ticket_status_logs (ticket_id TEXT, from_status TEXT, to_status TEXT, operator TEXT, changed_at REAL);
		INSERT INTO users VALUES ('u1', 'customer', 'customer', '客户'), ('u2', 'admin', 'admin', '管理员');
		INSERT INTO sessions VALUES ('legacy', 'u1', 'customer', unixepoch() + 60);
		INSERT INTO revoked_tokens VALUES ('revoked');
		INSERT INTO tickets VALUES
			('T1', 'old1', 'u1', '', '', '', '', 'pending_confirm', unixepoch(), '[]'),
			('T2', 'old2', 'u2', '', '', '', '', 'pending_confirm', unixepoch(), '[]');
	`)
	if err != nil {
		t.Fatal(err)
	}
	previousRepo, previousWriter := ticketRepo, ticketWriter
	t.Cleanup(func() { ticketRepo, ticketWriter = previousRepo, previousWriter })
	ticketWriter = tickets.NewWriter(conn)
	ticketRepo = tickets.NewRepository(conn)

	sign := func(user, id string) string {
		now := time.Now()
		value, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
			"iss": "ai-new", "aud": "ai-new-web", "sub": user, "typ": "access", "jti": id,
			"iat": now.Unix(), "exp": now.Add(time.Minute).Unix(),
		}).SignedString([]byte(secret))
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	handler := publicPendingTickets(conn)
	for _, tc := range []struct {
		name, token, query string
		status, total      int
		firstID            string
	}{
		{"anonymous", "", "", 401, 0, ""},
		{"revoked", sign("u1", "revoked"), "", 401, 0, ""},
		{"deleted user", sign("gone", "valid"), "", 401, 0, ""},
		{"customer scope", sign("u1", "valid"), "?all_users=true&user_id=u2", 200, 1, "T1"},
		{"legacy session", "legacy", "", 200, 1, "T1"},
		{"admin default", sign("u2", "valid"), "", 200, 1, "T2"},
		{"admin all", sign("u2", "valid"), "?all_users=true", 200, 2, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("GET", "/public/api/tickets/pending"+tc.query, nil)
			if tc.token != "" {
				r.Header.Set("Authorization", "Bearer "+tc.token)
			}
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatalf("status %d, body %s", w.Code, w.Body.String())
			}
			if tc.status != 200 {
				return
			}
			var response struct {
				Total   int
				Tickets []struct{ ID string }
			}
			if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response.Total != tc.total || len(response.Tickets) != tc.total {
				t.Fatalf("unexpected ticket count: %s", w.Body.String())
			}
			if tc.firstID != "" && response.Tickets[0].ID != tc.firstID {
				t.Fatalf("unexpected ticket: %s", w.Body.String())
			}
		})
	}
	for _, tc := range []struct {
		name, token, operation, body string
		status                       int
	}{
		{"anonymous write", "", "confirm", `{"ticket_id":"T1"}`, 401},
		{"revoked write", sign("u1", "revoked"), "confirm", `{"ticket_id":"T1"}`, 401},
		{"foreign ticket", "legacy", "cancel", `{"ticket_id":"T2"}`, 403},
		{"customer status", "legacy", "status", `{"ticket_id":"T1","status":"open"}`, 403},
		{"missing ticket", "legacy", "confirm", `{"ticket_id":"missing"}`, 404},
		{"owner confirm", "legacy", "confirm", `{"ticket_id":"T1","operator":"spoofed"}`, 200},
		{"repeat confirm", "legacy", "confirm", `{"ticket_id":"T1"}`, 200},
		{"administrator status", sign("u2", "admin"), "status", `{"ticket_id":"T1","status":"in_progress"}`, 200},
		{"same status", sign("u2", "admin"), "status", `{"ticket_id":"T1","status":"in_progress"}`, 409},
		{"administrator cancel", sign("u2", "admin"), "cancel", `{"ticket_id":"T2","reason":" test "}`, 200},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := httptest.NewRequest("POST", "/public/api/tickets/"+tc.operation, strings.NewReader(tc.body))
			if tc.token != "" {
				r.Header.Set("Authorization", "Bearer "+tc.token)
			}
			w := httptest.NewRecorder()
			publicTicketWrite(conn, tc.operation).ServeHTTP(w, r)
			if w.Code != tc.status {
				t.Fatalf("status %d, body %s", w.Code, w.Body.String())
			}
			if tc.status == 200 {
				var response struct {
					Ticket   map[string]any
					Identity *ticketUser
				}
				if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil || response.Ticket == nil ||
					(response.Identity != nil) != (tc.operation != "status") {
					t.Fatalf("unexpected response: %s", w.Body.String())
				}
			}
		})
	}
	var count int
	if err := conn.QueryRow("SELECT COUNT(*) FROM ticket_status_logs").Scan(&count); err != nil || count != 3 {
		t.Fatalf("expected three committed transitions, got %d (%v)", count, err)
	}
	var operator string
	if err := conn.QueryRow("SELECT operator FROM ticket_status_logs WHERE ticket_id = 'T1' AND to_status = 'open'").Scan(&operator); err != nil || operator != "客户" {
		t.Fatalf("expected authenticated operator, got %q (%v)", operator, err)
	}

	for index, tc := range []struct {
		token, body string
		status      int
		created     bool
	}{
		{"", `{"description":"新问题"}`, 401, false},
		{"legacy", `{"description":" "}`, 422, false},
		{"legacy", `{"description":"新问题","user_id":"u2","tried_steps":"重启"}`, 200, true},
		{"legacy", `{"description":" 新问题 "}`, 200, false},
	} {
		r := httptest.NewRequest("POST", "/public/api/tickets", strings.NewReader(tc.body))
		if tc.token != "" {
			r.Header.Set("Authorization", "Bearer "+tc.token)
		}
		w := httptest.NewRecorder()
		publicTicketCreate(conn).ServeHTTP(w, r)
		if w.Code != tc.status {
			t.Fatalf("case %d status %d, body %s", index, w.Code, w.Body.String())
		}
		if w.Code != 200 {
			continue
		}
		var result struct {
			Ticket   map[string]any
			Created  bool
			Identity ticketUser
			Message  string
		}
		if err := json.Unmarshal(w.Body.Bytes(), &result); err != nil || result.Identity.ID != "u1" ||
			result.Ticket["user_id"] != "u1" || result.Ticket["id"] != "T-1001" ||
			result.Ticket["tried_steps"] != "重启" || result.Created != tc.created || result.Message == "" {
			t.Fatalf("case %d unexpected response: %s", index, w.Body.String())
		}
	}

}
