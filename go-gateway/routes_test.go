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
		if !strings.HasPrefix(r.URL.Path, "/public/api/tickets/") ||
			r.Header.Get("Authorization") != "Bearer example" {
			t.Errorf("unexpected business request: %s", r.URL)
		}
		if r.Method == "GET" && r.URL.Query().Get("all_users") != "true" {
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
		{true, "POST", "/api/tickets/confirm", "go:/public/api/tickets/confirm"},
		{true, "POST", "/api/tickets/cancel", "go:/public/api/tickets/cancel"},
		{true, "POST", "/api/tickets/status", "go:/public/api/tickets/status"},
		{false, "POST", "/api/tickets/status", "python:/api/tickets/status"},
	} {
		cfg = config{businessURL: businessURL, frontendDir: dir, goPendingTickets: tc.enabled, goTicketWrites: tc.enabled}
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

}
