package main

import (
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
)

type agentTransport func(*http.Request) (*http.Response, error)

func (f agentTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestPublicChatPersistsOnlyCompleteTurns(t *testing.T) {
	conn, err := sql.Open("sqlite", "file:"+filepath.Join(t.TempDir(), "chat.db")+"?_txlock=immediate")
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	_, err = conn.Exec(`
		CREATE TABLE users (id TEXT, username TEXT, role TEXT, name TEXT);
		CREATE TABLE sessions (token TEXT, user_id TEXT, username TEXT, expires_at REAL);
		CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at REAL, updated_at REAL);
		CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT, role TEXT, content TEXT, at REAL);
		CREATE TABLE tried_steps (id TEXT PRIMARY KEY, user_id TEXT, step TEXT, at REAL);
		INSERT INTO users VALUES ('u1', 'customer', 'customer', '客户');
		INSERT INTO sessions VALUES ('legacy', 'u1', 'customer', unixepoch() + 60);
	`)
	if err != nil {
		t.Fatal(err)
	}
	chat := publicChat{
		store:    chatStore{read: conn, write: conn},
		agentURL: &url.URL{Scheme: "http", Host: "agent"}, token: "secret",
	}
	var upstreamBody string
	var historyLength int
	chat.client = &http.Client{Transport: agentTransport(func(r *http.Request) (*http.Response, error) {
		var input struct {
			UserID  string        `json:"user_id"`
			History []chatMessage `json:"history"`
		}
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil || input.UserID != "u1" ||
			len(input.History) != historyLength || r.Header.Get("X-Agent-Token") != "secret" {
			t.Errorf("unexpected Agent request: %+v, %v", input, err)
		}
		mediaType := "application/json"
		if r.URL.Path == "/internal/agent/stream" {
			mediaType = "text/event-stream"
		} else if r.URL.Path != "/internal/agent/reply" {
			t.Errorf("unexpected Agent path: %s", r.URL.Path)
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {mediaType}},
			Body: io.NopCloser(strings.NewReader(upstreamBody))}, nil
	})}
	post := func(stream bool) *httptest.ResponseRecorder {
		r := httptest.NewRequest("POST", "/api/chat", strings.NewReader(`{"message":"问题","user_id":"fake"}`))
		r.Header.Set("Authorization", "Bearer legacy")
		w := httptest.NewRecorder()
		chat.respond(w, r, stream)
		return w
	}

	upstreamBody = `{"reply":"答复","sources":[],"trace":[],"suggest_ticket":false,"tried_step":"已排查"}`
	if w := post(false); w.Code != 200 || strings.Contains(w.Body.String(), "tried_step") {
		t.Fatalf("unexpected reply: %d %s", w.Code, w.Body.String())
	}
	historyLength = 2
	upstreamBody = "event: delta\ndata: {\"text\":\"partial\"}\n\n"
	if w := post(true); !strings.Contains(w.Body.String(), "event: error") {
		t.Fatalf("expected interruption event: %s", w.Body.String())
	}
	upstreamBody = "event: retrieval\ndata: {\"sources\":[]}\n\nevent: delta\ndata: {\"text\":\"full\"}\n\nevent: done\ndata: {\"reply\":\"full\",\"suggest_ticket\":false}\n\n"
	if w := post(true); !w.Flushed || !strings.Contains(w.Body.String(), "event: done") {
		t.Fatalf("expected streamed completion: %s", w.Body.String())
	}

	var steps int
	if err := conn.QueryRow("SELECT COUNT(*) FROM tried_steps WHERE user_id = 'u1'").Scan(&steps); err != nil || steps != 1 {
		t.Fatalf("expected one recorded step, got %d: %v", steps, err)
	}
	var revision string
	for _, query := range []string{"?offset=0&limit=2", "?offset=2&limit=2"} {
		r := httptest.NewRequest("GET", "/api/conversation"+query, nil)
		r.Header.Set("Authorization", "Bearer legacy")
		w := httptest.NewRecorder()
		chat.handleConversation(w, r)
		var page struct {
			Messages []storedMessage `json:"messages"`
			Total    int             `json:"total"`
			Revision string          `json:"history_revision"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &page); err != nil || w.Code != 200 ||
			page.Total != 4 || len(page.Messages) != 2 || page.Revision == "" ||
			(revision != "" && page.Revision != revision) {
			t.Fatalf("unexpected history: %d %s", w.Code, w.Body.String())
		}
		revision = page.Revision
	}
	if _, err := conn.Exec("INSERT INTO conversations VALUES ('other-session', 'u2', '其他客户', 1, 1)"); err != nil {
		t.Fatal(err)
	}
	request := func(method, path, body string, handler http.HandlerFunc) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer legacy")
		w := httptest.NewRecorder()
		handler(w, r)
		return w
	}
	created := request("POST", "/api/conversations", "", chat.handleConversationCollection)
	var second conversationSummary
	if err := json.Unmarshal(created.Body.Bytes(), &second); err != nil || created.Code != 201 || second.ID == "" {
		t.Fatalf("create conversation: %d %s", created.Code, created.Body.String())
	}
	listed := request("GET", "/api/conversations", "", chat.handleConversationCollection)
	var list struct {
		Conversations []conversationSummary `json:"conversations"`
	}
	if err := json.Unmarshal(listed.Body.Bytes(), &list); err != nil || listed.Code != 200 || len(list.Conversations) != 2 || list.Conversations[0].ID != second.ID || list.Conversations[1].Title != "问题" {
		t.Fatalf("list conversations: %d %s", listed.Code, listed.Body.String())
	}
	firstID := list.Conversations[1].ID
	for _, stream := range []bool{false, true} {
		respond := func(w http.ResponseWriter, r *http.Request) { chat.respond(w, r, stream) }
		if w := request("POST", "/api/chat", `{"message":"越权","conversation_id":"other-session"}`, respond); w.Code != 404 {
			t.Fatalf("foreign conversation write: %d %s", w.Code, w.Body.String())
		}
	}
	if w := request("GET", "/api/conversation?conversation_id=other-session", "", chat.handleConversation); w.Code != 404 {
		t.Fatalf("foreign conversation read: %d %s", w.Code, w.Body.String())
	}
	historyLength = 0
	upstreamBody = `{"reply":"第二个答复"}`
	respond := func(w http.ResponseWriter, r *http.Request) { chat.respond(w, r, false) }
	if w := request("POST", "/api/chat", `{"message":"第二个问题","conversation_id":"`+second.ID+`"}`, respond); w.Code != 200 {
		t.Fatalf("reply in new conversation: %d %s", w.Code, w.Body.String())
	}
	first := request("GET", "/api/conversation?conversation_id="+firstID, "", chat.handleConversation)
	if first.Code != 200 || strings.Contains(first.Body.String(), "第二个") || !strings.Contains(first.Body.String(), "答复") {
		t.Fatalf("first conversation history changed: %d %s", first.Code, first.Body.String())
	}
	secondHistory := request("GET", "/api/conversation?conversation_id="+second.ID, "", chat.handleConversation)
	if secondHistory.Code != 200 || !strings.Contains(secondHistory.Body.String(), "第二个答复") {
		t.Fatalf("second conversation history: %d %s", secondHistory.Code, secondHistory.Body.String())
	}

}
