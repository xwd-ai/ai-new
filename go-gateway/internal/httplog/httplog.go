// Package httplog 提供结构化访问日志中间件。
//
// 为什么需要这个包：网关处于请求链路的最外层，是唯一能同时观测到
// 「进来的请求」和「出去的响应」的位置。把 method/path/status/duration
// 等字段用 slog 结构化输出，配合 X-Request-ID 透传，可以让一条请求
// 在多个服务之间的日志用同一个 request id 串联起来，这是排障时
// 定位跨服务问题的基本手段。
//
// 设计取舍：
//   - request id 优先复用上游传入的 X-Request-ID（跨服务串联），
//     缺失时本地生成短 id，避免引入对链路追踪系统的硬依赖；
//   - panic 在中间件层恢复并转成 500，保证单个 handler 的 bug
//     不会拖垮整个进程，同时把堆栈记进日志；
//   - 不在这里做采样/脱敏，日志策略交给 logger 本身（slog Handler）。
package httplog

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"net/http"
	"runtime/debug"
	"strconv"
	"time"
)

// HeaderRequestID 是 request id 的 HTTP 头，也是响应回写给客户端的头，
// 让客户端/下游可以在自己的日志里关联同一次请求。
const HeaderRequestID = "X-Request-ID"

// requestIDKey 是 context 中 request id 的私有键类型，
// 避免与其他包的 context 键冲突。
type requestIDKey struct{}

// WithRequestID 把 request id 放入 context，供下游 handler/日志关联。
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestIDFrom 从 context 取出 request id，不存在时返回空串。
func RequestIDFrom(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey{}).(string); ok {
		return v
	}
	return ""
}

// NewRequestID 生成 16 hex 字符（8 字节）的短 id。
//
// 取舍：选 crypto/rand 而不是 atomic 计数器——计数器虽然更快，但
// id 可被外部预测、且多实例部署下会重复；crypto/rand 的 8 字节
// 碰撞概率对请求级 tracing 足够低，且生成成本远低于一次 HTTP 处理。
func NewRequestID() string {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand 在正常系统上不会失败；退化成时间戳前缀，
		// 保证函数总有返回值，调用方无需处理错误。
		return fallbackRequestID()
	}
	return hex.EncodeToString(b[:])
}

// fallbackRequestID 仅在 crypto/rand 不可用时使用（理论上不会发生），
// 用纳秒时间戳保证单进程内唯一、可排序。
func fallbackRequestID() string {
	return strconv.FormatInt(time.Now().UnixNano(), 16)
}

// statusRecorder 包装 http.ResponseWriter 以捕获状态码与写入字节数。
//
// 关键点：net/http 允许 handler 不调用 WriteHeader 直接 Write，
// 此时状态码隐式为 200，必须在第一次 Write 时补记，否则日志里
// 状态码会是零值。
type statusRecorder struct {
	http.ResponseWriter
	status      int
	bytes       int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(code int) {
	if !r.wroteHeader {
		r.status = code
		r.wroteHeader = true
	}
	r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	if !r.wroteHeader {
		r.status = http.StatusOK // net/http 的隐式行为
		r.wroteHeader = true
	}
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

// Middleware 返回一个包装 http.Handler 的中间件：
// 注入/透传 X-Request-ID、记录结构化访问日志、恢复 panic。
//
// logger 为 nil 时使用 slog 默认 logger，避免调用方漏传时直接 panic。
func Middleware(logger *slog.Logger) func(http.Handler) http.Handler {
	if logger == nil {
		logger = slog.Default()
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()

			// 复用上游 request id 便于跨服务串联；没有则本地生成。
			id := r.Header.Get(HeaderRequestID)
			if id == "" {
				id = NewRequestID()
			}
			w.Header().Set(HeaderRequestID, id)
			ctx := WithRequestID(r.Context(), id)
			r = r.WithContext(ctx)

			rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}

			defer func() {
				if p := recover(); p != nil {
					// 响应头已写出则无法再改状态码，只记录；
					// 否则统一回 500，避免把内部错误细节泄漏给客户端。
					if !rec.wroteHeader {
						rec.WriteHeader(http.StatusInternalServerError)
					}
					logger.Error("panic recovered",
						slog.String("request_id", id),
						slog.Any("panic", p),
						slog.String("method", r.Method),
						slog.String("path", r.URL.Path),
						slog.String("stack", string(debug.Stack())),
					)
				}
				logger.Info("request",
					slog.String("request_id", id),
					slog.String("method", r.Method),
					slog.String("path", r.URL.Path),
					slog.Int("status", rec.status),
					slog.Int("bytes", rec.bytes),
					slog.Duration("duration", time.Since(start)),
					slog.String("remote_addr", r.RemoteAddr),
				)
			}()

			next.ServeHTTP(rec, r)
		})
	}
}
