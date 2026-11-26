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
		method, path, want string
	}{
		{"GET", "/", "index.html"},
		{"GET", "/admin", "admin.html"},
		{"GET", "/assets/chat.js", "chat.js"},
		{"GET", "/api/tickets/pending?all_users=true", "go:/public/api/tickets/pending"},
		{"GET", "/api/chat", "python:/api/chat"},
		{"GET", "/api/me", "go:/public/api/me"},
		{"POST", "/api/refresh", "go:/public/api/refresh"},
		{"POST", "/api/logout", "go:/public/api/logout"},
		{"POST", "/api/sessions/cleanup", "go:/public/api/sessions/cleanup"},
		{"POST", "/api/register", "go:/public/api/register"},
		{"GET", "/api/sources/ref-1", "go:/public/api/sources/ref-1"},
		{"GET", "/api/knowledge/settings", "go:/public/api/knowledge/settings"},
		{"PUT", "/api/knowledge/settings", "go:/public/api/knowledge/settings"},
		{"GET", "/api/knowledge/items", "go:/public/api/knowledge/items"},
		{"GET", "/api/knowledge/ingest/pending", "go:/public/api/knowledge/ingest/pending"},
		{"GET", "/api/order", "go:/public/api/order"},
		{"GET", "/api/order/list", "go:/public/api/order/list"},
		{"GET", "/api/order/admin", "go:/public/api/order/admin"},
		{"GET", "/api/order/admin/O-1", "go:/public/api/order/admin/O-1"},
		{"GET", "/api/order/scope", "go:/public/api/order/scope"},
		{"GET", "/api/order/feature-scope", "go:/public/api/order/feature-scope"},
		{"GET", "/api/order/renewal", "go:/public/api/order/renewal"},
		{"GET", "/api/tickets", "go:/public/api/tickets"},
		{"GET", "/api/tickets/stats", "go:/public/api/tickets/stats"},
		{"POST", "/api/order/expire", "go:/public/api/order/expire"},
		{"POST", "/api/tickets/batch-close", "go:/public/api/tickets/batch-close"},
		{"POST", "/api/tickets/batch-transfer", "go:/public/api/tickets/batch-transfer"},
		{"POST", "/api/tickets/transfer", "go:/public/api/tickets/transfer"},
		{"GET", "/api/tickets/T-1", "go:/public/api/tickets/T-1"},
		{"GET", "/api/tickets/T-1/transfer-decision", "go:/public/api/tickets/T-1/transfer-decision"},
		{"POST", "/api/tickets/reply", "go:/public/api/tickets/reply"},
		{"POST", "/api/tickets/expire", "go:/public/api/tickets/expire"},
		{"POST", "/api/knowledge/ingest", "python:/api/knowledge/ingest"},
		{"POST", "/api/login", "go:/public/api/login"},
		{"GET", "/api/conversation", "go:/public/api/conversation"},
		{"POST", "/api/chat", "go:/public/api/chat"},
		{"POST", "/api/chat/stream", "go:/public/api/chat/stream"},
		{"POST", "/api/tickets", "go:/public/api/tickets"},
		{"POST", "/api/tickets/confirm", "go:/public/api/tickets/confirm"},
		{"POST", "/api/tickets/cancel", "go:/public/api/tickets/cancel"},
		{"POST", "/api/tickets/status", "go:/public/api/tickets/status"},
	} {
		cfg = config{businessURL: businessURL, frontendDir: dir}
		mux := http.NewServeMux()
		registerRoutes(mux)
		r := httptest.NewRequest(tc.method, tc.path, nil)
		r.Header.Set("Authorization", "Bearer example")
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, r)
		if w.Code != http.StatusOK || w.Body.String() != tc.want {
			t.Errorf("%s got %d %q, want %q", tc.path, w.Code, w.Body.String(), tc.want)
		}
	}

	businessProxy.Transport = routeTransport(func(*http.Request) (*http.Response, error) {
		return nil, errors.New("upstream connection closed")
	})
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

	businessProxy = nil
	w = httptest.NewRecorder()
	mux.ServeHTTP(w, httptest.NewRequest("POST", "/api/login", nil))
	if w.Code != http.StatusServiceUnavailable {
		t.Fatalf("missing Go backend must return 503, got %d", w.Code)
	}

}
