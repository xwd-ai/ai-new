package httplog

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// newTestLogger 返回写进 buffer 的 JSON slog logger，并附一个
// 从日志记录里按字段取值的辅助函数，便于断言结构化字段。
func newTestLogger() (*bytes.Buffer, *slog.Logger) {
	var buf bytes.Buffer
	h := slog.NewJSONHandler(&buf, &slog.HandlerOptions{Level: slog.LevelDebug})
	return &buf, slog.New(h)
}

func lastRecord(t *testing.T, buf *bytes.Buffer) map[string]any {
	t.Helper()
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) == 0 || lines[len(lines)-1] == "" {
		t.Fatal("没有日志输出")
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(lines[len(lines)-1]), &m); err != nil {
		t.Fatalf("解析日志 JSON 失败: %v", err)
	}
	return m
}

func TestMiddlewareNormalRequest(t *testing.T) {
	buf, logger := newTestLogger()
	var gotID string
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotID = RequestIDFrom(r.Context())
		w.WriteHeader(http.StatusTeapot)
		_, _ = w.Write([]byte("hello"))
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/ping", nil))

	if rec.Code != http.StatusTeapot {
		t.Errorf("status = %d, want %d", rec.Code, http.StatusTeapot)
	}
	if gotID == "" {
		t.Error("handler 内取不到 request id")
	}
	if got := rec.Header().Get(HeaderRequestID); got != gotID {
		t.Errorf("响应头 request id = %q, want %q", got, gotID)
	}

	m := lastRecord(t, buf)
	if m["method"] != http.MethodGet {
		t.Errorf("method = %v, want GET", m["method"])
	}
	if m["path"] != "/ping" {
		t.Errorf("path = %v, want /ping", m["path"])
	}
	if m["status"] != float64(http.StatusTeapot) {
		t.Errorf("status = %v, want %d", m["status"], http.StatusTeapot)
	}
	if m["bytes"] != float64(len("hello")) {
		t.Errorf("bytes = %v, want %d", m["bytes"], len("hello"))
	}
	if _, ok := m["duration"]; !ok {
		t.Error("日志缺少 duration 字段")
	}
	if _, ok := m["remote_addr"]; !ok {
		t.Error("日志缺少 remote_addr 字段")
	}
}

func TestMiddlewareReuseIncomingRequestID(t *testing.T) {
	buf, logger := newTestLogger()
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if id := RequestIDFrom(r.Context()); id != "upstream-42" {
			t.Errorf("ctx 中的 request id = %q, want upstream-42", id)
		}
	}))

	req := httptest.NewRequest(http.MethodPost, "/x", nil)
	req.Header.Set(HeaderRequestID, "upstream-42")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if got := rec.Header().Get(HeaderRequestID); got != "upstream-42" {
		t.Errorf("响应头应复用上游 id, got %q", got)
	}
	if m := lastRecord(t, buf); m["request_id"] != "upstream-42" {
		t.Errorf("日志 request_id = %v, want upstream-42", m["request_id"])
	}
}

func TestMiddlewarePanic(t *testing.T) {
	buf, logger := newTestLogger()
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		panic("boom")
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/crash", nil))

	if rec.Code != http.StatusInternalServerError {
		t.Errorf("panic 后 status = %d, want 500", rec.Code)
	}
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	var panicRec, requestRec map[string]any
	for _, line := range lines {
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("解析日志失败: %v", err)
		}
		if m["msg"] == "panic recovered" {
			panicRec = m
		}
		if m["msg"] == "request" {
			requestRec = m
		}
	}
	if panicRec == nil {
		t.Fatal("缺少 panic recovered 日志")
	}
	if panicRec["panic"] != "boom" {
		t.Errorf("panic 字段 = %v, want boom", panicRec["panic"])
	}
	if s, _ := panicRec["stack"].(string); !strings.Contains(s, "TestMiddlewarePanic") {
		t.Errorf("stack 缺少调用栈信息: %q", s)
	}
	if requestRec == nil || requestRec["status"] != float64(http.StatusInternalServerError) {
		t.Errorf("panic 后访问日志应为 500, got %v", requestRec["status"])
	}
}

func TestMiddlewarePanicAfterHeaderWritten(t *testing.T) {
	buf, logger := newTestLogger()
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		panic("late boom")
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/late", nil))

	// 响应头已写出，状态码不能再改成 500。
	if rec.Code != http.StatusCreated {
		t.Errorf("status = %d, want 201（不覆盖已写出的头）", rec.Code)
	}
	if !strings.Contains(buf.String(), "late boom") {
		t.Error("应记录 panic 日志")
	}
}

func TestMiddlewareWriteWithoutWriteHeader(t *testing.T) {
	buf, logger := newTestLogger()
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("implicit")) // 不调用 WriteHeader
	}))

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/implicit", nil))

	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200（隐式默认）", rec.Code)
	}
	if m := lastRecord(t, buf); m["status"] != float64(http.StatusOK) {
		t.Errorf("日志 status = %v, want 200", m["status"])
	}
}

func TestMiddlewareConcurrentRequests(t *testing.T) {
	_, logger := newTestLogger()
	h := Middleware(logger)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))

	var wg sync.WaitGroup
	ids := make([]string, 50)
	for i := range ids {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/c", nil))
			ids[i] = rec.Header().Get(HeaderRequestID)
		}(i)
	}
	wg.Wait()

	seen := make(map[string]bool)
	for _, id := range ids {
		if id == "" {
			t.Fatal("并发下出现空 request id")
		}
		seen[id] = true
	}
	if len(seen) != len(ids) {
		t.Errorf("并发下 request id 有重复: %d/%d 唯一", len(seen), len(ids))
	}
}

func TestRequestIDContext(t *testing.T) {
	ctx := WithRequestID(context.Background(), "abc")
	if got := RequestIDFrom(ctx); got != "abc" {
		t.Errorf("RequestIDFrom = %q, want abc", got)
	}
	if got := RequestIDFrom(context.Background()); got != "" {
		t.Errorf("空 context 应返回空串, got %q", got)
	}
}

func TestNewRequestIDUniqueAndFormat(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 1000; i++ {
		id := NewRequestID()
		if len(id) != 16 { // 8 字节 hex
			t.Fatalf("id 长度 = %d, want 16: %q", len(id), id)
		}
		if _, err := hex.DecodeString(id); err != nil {
			t.Fatalf("id 不是合法 hex: %v", err)
		}
		seen[id] = true
	}
	if len(seen) != 1000 {
		t.Errorf("1000 次 NewRequestID 出现重复: %d 唯一", len(seen))
	}
}
