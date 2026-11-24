// Package stream 提供 Server-Sent Events 的最小实现。
//
// 只做三件事：写 SSE 格式、每条消息后 Flush、并发安全。
// 业务语义（推什么事件）留给调用方，这里不掺入任何领域逻辑。
package stream

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
)

// ErrNoFlusher 表示底层 ResponseWriter 不支持 Flush，无法做流式输出。
var ErrNoFlusher = errors.New("底层 ResponseWriter 不支持 Flush，无法流式输出")

// Writer 封装 text/event-stream 的写入。
//
// 加锁的原因：心跳协程与业务协程会同时写同一个 ResponseWriter，
// 而 SSE 的消息边界（空行）必须成对写入，交错写会拼出非法帧。
type Writer struct {
	mu      sync.Mutex
	w       http.ResponseWriter
	flusher http.Flusher
}

// NewWriter 校验 Flush 能力并写入 SSE 必需的响应头。
func NewWriter(w http.ResponseWriter) (*Writer, error) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		return nil, ErrNoFlusher
	}

	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache")
	h.Set("Connection", "keep-alive")
	// 让 nginx 一类的反向代理不要缓冲，否则客户端会攒一大块才收到
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	return &Writer{w: w, flusher: flusher}, nil
}

// Send 发送一条事件并立即刷新。
func (s *Writer) Send(id int, event string, payload any) error {
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	if _, err := fmt.Fprintf(s.w, "id: %d\nevent: %s\ndata: %s\n\n", id, event, data); err != nil {
		return err
	}
	s.flusher.Flush()
	return nil
}

// Comment 发送心跳注释行。
//
// SSE 规范规定以冒号开头的行是注释，客户端会忽略；
// 但中间的空白行仍会触发连接活动，因此常用作 keep-alive，
// 避免长时间无数据被代理或负载均衡判定为空闲连接而断开。
func (s *Writer) Comment(text string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	if _, err := fmt.Fprintf(s.w, ": %s\n\n", text); err != nil {
		return err
	}
	s.flusher.Flush()
	return nil
}
