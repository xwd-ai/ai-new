package stream

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestWriterHeadersAndFrames(t *testing.T) {
	rec := httptest.NewRecorder()
	w, err := NewWriter(rec)
	if err != nil {
		t.Fatalf("创建 Writer 失败: %v", err)
	}

	if err := w.Send(1, "start", map[string]string{"q": "导出"}); err != nil {
		t.Fatalf("发送事件失败: %v", err)
	}

	// 响应头必须是 SSE 约定的那几个
	headers := map[string]string{
		"Content-Type":  "text/event-stream",
		"Cache-Control": "no-cache",
		"Connection":    "keep-alive",
	}
	for key, want := range headers {
		if got := rec.Header().Get(key); !strings.Contains(got, want) {
			t.Errorf("响应头 %s = %q，期望包含 %q", key, got, want)
		}
	}

	body := rec.Body.String()
	// 帧格式：id / event / data 三段 + 结尾空行
	for _, want := range []string{"id: 1\n", "event: start\n", `data: {"q":"导出"}` + "\n\n"} {
		if !strings.Contains(body, want) {
			t.Errorf("响应体缺少 %q，实际：%q", want, body)
		}
	}
}

func TestWriterComment(t *testing.T) {
	rec := httptest.NewRecorder()
	w, err := NewWriter(rec)
	if err != nil {
		t.Fatalf("创建 Writer 失败: %v", err)
	}

	if err := w.Comment("keep-alive"); err != nil {
		t.Fatalf("发送心跳失败: %v", err)
	}

	// 注释行以冒号开头，客户端会忽略；后面仍要有空行分隔
	if got := rec.Body.String(); !strings.Contains(got, ": keep-alive\n\n") {
		t.Fatalf("心跳格式不正确：%q", got)
	}
}

func TestNewWriterRejectsNonFlusher(t *testing.T) {
	// httptest.ResponseRecorder 支持 Flush，这里用一个不含 Flush 的包装验证拒绝路径
	if _, err := NewWriter(nonFlusherWriter{}); err == nil {
		t.Fatal("不支持 Flush 的 ResponseWriter 应返回错误")
	}
}

// nonFlusherWriter 只实现 WriteHeader/Write，不实现 Flush。
type nonFlusherWriter struct{}

func (nonFlusherWriter) Header() http.Header       { return http.Header{} }
func (nonFlusherWriter) Write([]byte) (int, error) { return 0, nil }
func (nonFlusherWriter) WriteHeader(int)           {}
