// Package ratelimit 提供按 key 隔离的 token bucket 限流。
//
// 为什么需要这个包：网关直接面对公网流量，必须在上游业务之前
// 保护后端——按 key（user_id 或匿名 IP）隔离的限流保证一个滥用者
// 不会耗尽全局限额。token bucket（而非固定窗口）允许短时突发，
// 又用补充速率限制平均流量，是对 API 网关最常用的折中。
//
// 设计取舍：
//   - 复用 golang.org/x/time/rate 的 *rate.Limiter 而非自研令牌桶，
//     它的补充算法基于惰性计算（每次 Allow 才推进时间），没有
//     后台刷新 token 的 goroutine，正确性久经验证；
//   - key -> limiter 用 sync.Map：读多写少（同 key 的后续请求只是
//     命中已存在的 entry），且清理 goroutine 可以 Range 遍历，
//     避免为每个 key 加锁；
//   - IdleTTL + 后台清理：网关的 key 空间是开放的（IP/匿名用户），
//     不清理会导致 map 无限增长，这是长期运行服务必须处理的问题。
package ratelimit

import (
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/time/rate"
)

// Config 配置 Limiter。
type Config struct {
	// Limit 是 token 每秒补充速率（token/s）。
	Limit rate.Limit
	// Burst 是桶容量，也决定允许的最大瞬时突发量。
	Burst int
	// IdleTTL 是桶的空闲回收阈值：超过该时长未使用的桶会被清理。
	IdleTTL time.Duration
}

// bucket 在 *rate.Limiter 之外记录最后使用时间，供清理 goroutine 判断。
type bucket struct {
	limiter  *rate.Limiter
	lastUsed atomic.Int64 // unix nano
}

// Limiter 按 key 维护独立的 token bucket。
type Limiter struct {
	cfg     Config
	buckets sync.Map // key string -> *bucket

	closed   atomic.Bool
	stopCh   chan struct{}
	stopOnce sync.Once
}

// New 创建 Limiter 并启动后台清理 goroutine。
// IdleTTL <= 0 时按 1 分钟处理（仍然清理，避免内存泄漏是硬要求）。
func New(cfg Config) *Limiter {
	if cfg.IdleTTL <= 0 {
		cfg.IdleTTL = time.Minute
	}
	l := &Limiter{
		cfg:    cfg,
		stopCh: make(chan struct{}),
	}
	go l.cleanup()
	return l
}

// Allow 报告 key 当前是否允许一次请求（等价于 AllowN(key, 1)）。
func (l *Limiter) Allow(key string) bool {
	return l.AllowN(key, 1)
}

// AllowN 报告 key 当前是否允许一次消耗 n 个 token 的请求。
func (l *Limiter) AllowN(key string, n int) bool {
	b := l.bucketFor(key)
	return b.limiter.AllowN(time.Now(), n)
}

// bucketFor 取出或惰性创建 key 对应的桶，并刷新 lastUsed。
func (l *Limiter) bucketFor(key string) *bucket {
	now := time.Now().UnixNano()
	if v, ok := l.buckets.Load(key); ok {
		b := v.(*bucket)
		b.lastUsed.Store(now)
		return b
	}
	// LoadOrAdd 保证并发下同 key 只创建一个 limiter，
	// 后到的创建结果被丢弃，不会出现双桶。
	b := &bucket{limiter: rate.NewLimiter(l.cfg.Limit, l.cfg.Burst)}
	b.lastUsed.Store(now)
	actual, loaded := l.buckets.LoadOrStore(key, b)
	act := actual.(*bucket)
	if loaded { // 已有并发创建的桶，用旧的并刷新时间
		act.lastUsed.Store(now)
	}
	return act
}

// cleanup 周期性删除超过 IdleTTL 未使用的桶。
//
// 为什么用 Ticker + Range 而不是逐桶定时器：key 数量可能很大，
// 逐桶 timer 的内存和调度开销远高于一次批量扫描；扫描间隔取
// IdleTTL 的一半，保证桶最迟在 IdleTTL~1.5*IdleTTL 之间被回收。
func (l *Limiter) cleanup() {
	interval := l.cfg.IdleTTL / 2
	if interval < 10*time.Millisecond {
		interval = 10 * time.Millisecond
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-l.stopCh:
			return
		case <-ticker.C:
			threshold := time.Now().Add(-l.cfg.IdleTTL).UnixNano()
			l.buckets.Range(func(key, v any) bool {
				b := v.(*bucket)
				if b.lastUsed.Load() < threshold {
					l.buckets.Delete(key)
				}
				return true
			})
		}
	}
}

// Close 停止清理 goroutine。幂等：多次调用安全。
// Close 后 Allow 仍可用（只是不再清理），不 panic——
// 优雅关闭时可能仍有 in-flight 请求。
func (l *Limiter) Close() {
	l.stopOnce.Do(func() {
		l.closed.Store(true)
		close(l.stopCh)
	})
}

// Middleware 返回限流中间件：keyFunc 取限流 key（可注入），
// 超限返回 429 + Retry-After。keyFunc 为 nil 时用 DefaultKeyFunc。
//
// Retry-After 取 1 秒：x/time/rate 不暴露下次可用的等待时长，
// 取一个保守的小值，客户端可据此退避重试。
func Middleware(l *Limiter, keyFunc func(*http.Request) string) func(http.Handler) http.Handler {
	if keyFunc == nil {
		keyFunc = DefaultKeyFunc
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !l.Allow(keyFunc(r)) {
				w.Header().Set("Retry-After", "1")
				http.Error(w, "rate limit exceeded", http.StatusTooManyRequests)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// DefaultKeyFunc 从请求提取限流 key：
// 优先信任网关内部认证层注入的 X-Auth-User（已验证的用户 id），
// 否则退化为客户端 IP。对代理场景只取 RemoteAddr 的 host 部分，
// 剥掉端口。
func DefaultKeyFunc(r *http.Request) string {
	if u := r.Header.Get("X-Auth-User"); u != "" {
		return "user:" + u
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	return "ip:" + host
}
