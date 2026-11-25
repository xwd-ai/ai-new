package main

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type routeTransport func(*http.Request) (*http.Response, error)

func (f routeTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	return f(r)
}

func TestGoMigrationRoutes(t *testing.T) {
	oldConfig, oldLogger, oldBackend, oldBusiness := cfg, logger, backendProxy, businessProxy
	t.Cleanup(func() { cfg, logger, backendProxy, businessProxy = oldConfig, oldLogger, oldBackend, oldBusiness })
	logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	pythonURL := &url.URL{Scheme: "http", Host: "python"}
	businessURL := &url.URL{Scheme: "http", Host: "business"}
	backendProxy = newBackendProxy(pythonURL)
	businessProxy = newBackendProxy(businessURL)
	backendProxy.Transport = routeTransport(func(r *http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader("python:" + r.URL.Path))}, nil
	})
	businessProxy.Transport = routeTransport(func(r *http.Request) (*http.Response, error) {
		if !strings.HasPrefix(r.URL.Path, "/public/api/") ||
			r.Header.Get("Authorization") != "Bearer example" {
			t.Errorf("unexpected business request: %s", r.URL)
		}
		if r.URL.Path == "/public/api/tickets/pending" && r.URL.Query().Get("all_users") != "true" {
			t.Errorf("missing pending query: %s", r.URL)
		}
		return &http.Response{StatusCode: http.StatusOK, Header: make(http.Header),
			Body: io.NopCloser(strings.NewReader("go:" + r.URL.Path))}, nil
	})

	dir := t.TempDir()
	for _, name := range []string{"index.html", "admin.html", "chat.js"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(name), 0600); err != nil {
			t.Fatal(err)
		}
	}
	for _, tc := range []struct {
		enabled            bool
		method, path, want string
	}{
		{true, "GET", "/", "index.html"},
		{true, "GET", "/admin", "admin.html"},
		{true, "GET", "/assets/chat.js", "chat.js"},
		{true, "GET", "/api/tickets/pending?all_users=true", "go:/public/api/tickets/pending"},
		{false, "GET", "/api/tickets/pending?all_users=true", "python:/api/tickets/pending"},
		{true, "GET", "/api/chat", "python:/api/chat"},
		{true, "GET", "/api/me", "go:/public/api/me"},
		{true, "POST", "/api/refresh", "go:/public/api/refresh"},
		{false, "POST", "/api/refresh", "python:/api/refresh"},
		{true, "POST", "/api/logout", "go:/public/api/logout"},
		{true, "POST", "/api/sessions/cleanup", "go:/public/api/sessions/cleanup"},
		{false, "POST", "/api/logout", "python:/api/logout"},
		{true, "POST", "/api/register", "go:/public/api/register"},
		{true, "GET", "/api/sources/ref-1", "go:/public/api/sources/ref-1"},
		{true, "GET", "/api/knowledge/settings", "go:/public/api/knowledge/settings"},
		{true, "PUT", "/api/knowledge/settings", "go:/public/api/knowledge/settings"},
		{true, "GET", "/api/knowledge/items", "go:/public/api/knowledge/items"},
		{true, "GET", "/api/knowledge/ingest/pending", "go:/public/api/knowledge/ingest/pending"},
		{true, "GET", "/api/order", "go:/public/api/order"},
		{true, "GET", "/api/order/list", "go:/public/api/order/list"},
		{true, "GET", "/api/order/admin", "go:/public/api/order/admin"},
		{true, "GET", "/api/order/admin/O-1", "go:/public/api/order/admin/O-1"},
		{true, "GET", "/api/order/scope", "go:/public/api/order/scope"},
		{true, "GET", "/api/order/feature-scope", "go:/public/api/order/feature-scope"},
		{true, "GET", "/api/order/renewal", "go:/public/api/order/renewal"},
		{true, "GET", "/api/tickets", "go:/public/api/tickets"},
		{true, "GET", "/api/tickets/stats", "go:/public/api/tickets/stats"},
		{true, "POST", "/api/order/expire", "go:/public/api/order/expire"},
		{true, "POST", "/api/tickets/batch-close", "go:/public/api/tickets/batch-close"},
		{true, "POST", "/api/tickets/batch-transfer", "go:/public/api/tickets/batch-transfer"},
		{true, "POST", "/api/tickets/transfer", "go:/public/api/tickets/transfer"},
		{true, "GET", "/api/tickets/T-1", "go:/public/api/tickets/T-1"},
		{true, "GET", "/api/tickets/T-1/transfer-decision", "go:/public/api/tickets/T-1/transfer-decision"},
		{true, "POST", "/api/tickets/reply", "go:/public/api/tickets/reply"},
		{true, "POST", "/api/tickets/expire", "go:/public/api/tickets/expire"},
		{false, "POST", "/api/register", "python:/api/register"},
		{true, "POST", "/api/knowledge/ingest", "python:/api/knowledge/ingest"},
		{true, "POST", "/api/login", "go:/public/api/login"},
		{false, "POST", "/api/login", "python:/api/login"},
		{true, "GET", "/api/conversation", "go:/public/api/conversation"},
		{true, "POST", "/api/chat", "go:/public/api/chat"},
		{true, "POST", "/api/chat/stream", "go:/public/api/chat/stream"},
		{false, "POST", "/api/chat", "python:/api/chat"},
		{true, "POST", "/api/tickets", "go:/public/api/tickets"},
		{false, "POST", "/api/tickets", "python:/api/tickets"},
		{true, "POST", "/api/tickets/confirm", "go:/public/api/tickets/confirm"},
		{true, "POST", "/api/tickets/cancel", "go:/public/api/tickets/cancel"},
		{true, "POST", "/api/tickets/status", "go:/public/api/tickets/status"},
		{false, "POST", "/api/tickets/status", "python:/api/tickets/status"},
	} {
		cfg = config{businessURL: businessURL, frontendDir: dir, goPendingTickets: tc.enabled, goTicketWrites: tc.enabled, goTicketCreate: tc.enabled, goChat: tc.enabled, goSessionReadRefresh: tc.enabled, goLogin: tc.enabled, goSessionRevocation: tc.enabled, goBusinessAPI: tc.enabled}
		mux := http.NewServeMux()
		registerRoutes(mux)
		r := httptest.NewRequest(tc.method, tc.path, nil)
		r.Header.Set("Authorization", "Bearer example")
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		if w.Code != http.StatusOK || w.Body.String() != tc.want {
			t.Errorf("%s enabled=%v got %d %q, want %q", tc.path, tc.enabled, w.Code, w.Body.String(), tc.want)
		}
	}

	cfg = config{businessURL: businessURL, goBusinessAPI: true}
	pendingOverrideMux := http.NewServeMux()
	registerRoutes(pendingOverrideMux)
	pending := httptest.NewRecorder()
	pendingOverrideMux.ServeHTTP(pending, httptest.NewRequest("GET", "/api/tickets/pending", nil))
	if pending.Code != http.StatusOK || pending.Body.String() != "python:/api/tickets/pending" {
		t.Fatalf("pending override was captured by detail route: %d %q", pending.Code, pending.Body.String())
	}

	businessProxy.Transport = routeTransport(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("upstream connection closed")
	})
	cfg.goTicketWrites = true
	mux := http.NewServeMux()
	registerRoutes(mux)
	w := httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("POST", "/api/tickets/status", nil))
	if w.Code != http.StatusBadGateway {
		t.Fatalf("failed write must return 502 without replaying through Python, got %d", w.Code)
	}

	w = httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("POST", "/internal/agent/reply", nil))
	if w.Code != http.StatusNotFound {
		t.Fatalf("Agent route exposed: %d", w.Code)
	}

}

func TestGoPrimaryDefaultsAndOverrides(t *testing.T) {
	for _, name := range []string{
		"GATEWAY_GO_PENDING_TICKETS", "GATEWAY_GO_TICKET_WRITES", "GATEWAY_GO_TICKET_CREATE",
		"GATEWAY_GO_CHAT", "GATEWAY_GO_SESSION_REFRESH", "GATEWAY_GO_LOGIN",
		"GATEWAY_GO_SESSION_REVOCATION", "GATEWAY_GO_BUSINESS_API",
	} {
		t.Setenv(name, "")
	}
	t.Setenv("GATEWAY_GO_PRIMARY", "true")
	t.Setenv("GATEWAY_GO_CHAT", "false")
	c := loadConfig()
	if !c.goPendingTickets || !c.goTicketWrites || !c.goTicketCreate ||
		!c.goSessionReadRefresh || !c.goLogin || !c.goSessionRevocation || !c.goBusinessAPI || c.goChat {
		t.Fatalf("unexpected primary route configuration: %+v", c)
	}
}
