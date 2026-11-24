package ratelimit

import (
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/time/rate"
)

func countBuckets(l *Limiter) int {
	n := 0
	l.buckets.Range(func(_, _ any) bool {
		n++
		return true
	})
	return n
}

func TestBucketBurstAndRefill(t *testing.T) {
	// Burst=3, 补充速率 20 token/s（50ms 一个）。
	// 计时测试用宽松的阈值避免 flaky。
	l := New(Config{Limit: 20, Burst: 3, IdleTTL: time.Hour})
	defer l.Close()

	for i := 0; i < 3; i++ {
		if !l.Allow("k") {
			t.Fatalf("第 %d 次应放行（桶容量 3）", i+1)
		}
	}
	if l.Allow("k") {
		t.Error("超过桶容量后应拒绝")
	}

	// 等待补充至少 1 个 token：100ms * 20/s = 2 个。
	time.Sleep(100 * time.Millisecond)
	if !l.Allow("k") {
		t.Error("等待补充后应放行")
	}
}

func TestAllowN(t *testing.T) {
	l := New(Config{Limit: rate.Limit(0), Burst: 5, IdleTTL: time.Hour})
	defer l.Close()

	if !l.AllowN("k", 3) {
		t.Error("消耗 3 个 token（桶容量 5）应放行")
	}
	if l.AllowN("k", 3) {
		t.Error("剩余 2 个 token，再要 3 个应拒绝")
	}
	if !l.AllowN("k", 2) {
		t.Error("剩余 2 个 token，要 2 个应放行")
	}
}

func TestDifferentKeysIndependent(t *testing.T) {
	l := New(Config{Limit: rate.Limit(0), Burst: 1, IdleTTL: time.Hour})
	defer l.Close()

	if !l.Allow("alice") {
		t.Error("alice 首次应放行")
	}
	if l.Allow("alice") {
		t.Error("alice 二次应拒绝（桶容量 1）")
	}
	if !l.Allow("bob") {
		t.Error("bob 不应受 alice 的桶影响")
	}
}

func TestIdleTTLCleanup(t *testing.T) {
	// IdleTTL=30ms，清理间隔 max(15ms,10ms)=15ms，
	// 桶最迟约 45ms 后被回收；这里等 200ms 留足余量。
	l := New(Config{Limit: rate.Limit(0), Burst: 1, IdleTTL: 30 * time.Millisecond})
	defer l.Close()

	l.Allow("a")
	l.Allow("b")
	if got := countBuckets(l); got != 2 {
		t.Fatalf("应有 2 个桶, got %d", got)
	}
	time.Sleep(200 * time.Millisecond)
	if got := countBuckets(l); got != 0 {
		t.Errorf("空闲桶应被清理, 残留 %d 个", got)
	}

	// 清理后同 key 再次使用应重新建桶并放行。
	if !l.Allow("a") {
		t.Error("清理后重建的桶应放行")
	}
}

func TestActiveBucketNotCleaned(t *testing.T) {
	l := New(Config{Limit: rate.Limit(0), Burst: 100, IdleTTL: 40 * time.Millisecond})
	defer l.Close()

	// 持续使用中的桶不应被清理。
	deadline := time.Now().Add(300 * time.Millisecond)
	for time.Now().Before(deadline) {
		l.Allow("hot")
		time.Sleep(10 * time.Millisecond)
	}
	if got := countBuckets(l); got != 1 {
		t.Errorf("活跃桶不应被清理, got %d 个桶", got)
	}
}

func TestCloseIdempotentAndAllowStillWorks(t *testing.T) {
	l := New(Config{Limit: 1, Burst: 1, IdleTTL: time.Hour})
	l.Close()
	l.Close() // 幂等

	// Close 后 Allow 不 panic，功能仍可用（只是不再清理）。
	done := make(chan bool, 1)
	go func() {
		done <- l.Allow("k")
	}()
	select {
	case ok := <-done:
		if !ok {
			t.Error("Close 后 Allow 应正常放行")
		}
	case <-time.After(time.Second):
		t.Fatal("Close 后 Allow 卡死")
	}
}

func TestMiddleware429(t *testing.T) {
	l := New(Config{Limit: rate.Limit(0), Burst: 1, IdleTTL: time.Hour})
	defer l.Close()

	handler := Middleware(l, nil)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "10.0.0.1:1234"
	req.Header.Set("X-Auth-User", "u1")

	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Errorf("首次请求 status = %d, want 200", rec.Code)
	}

	rec2 := httptest.NewRecorder()
	handler.ServeHTTP(rec2, req)
	if rec2.Code != http.StatusTooManyRequests {
		t.Errorf("超限 status = %d, want 429", rec2.Code)
	}
	if got := rec2.Header().Get("Retry-After"); got == "" {
		t.Error("超限响应应带 Retry-After 头")
	}
}

func TestDefaultKeyFunc(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/", nil)
	req.RemoteAddr = "1.2.3.4:5678"
	if got := DefaultKeyFunc(req); got != "ip:1.2.3.4" {
		t.Errorf("匿名 key = %q, want ip:1.2.3.4", got)
	}
	req.Header.Set("X-Auth-User", "alice")
	if got := DefaultKeyFunc(req); got != "user:alice" {
		t.Errorf("用户 key = %q, want user:alice", got)
	}
}

func TestConcurrentSameAndDifferentKeys(t *testing.T) {
	l := New(Config{Limit: 1000, Burst: 100000, IdleTTL: time.Hour})
	defer l.Close()

	var wg sync.WaitGroup
	var allowed, denied atomic.Int64
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := "k" + string(rune('a'+i%3))
			for j := 0; j < 100; j++ {
				if l.Allow(key) {
					allowed.Add(1)
				} else {
					denied.Add(1)
				}
			}
		}(i)
	}
	wg.Wait()
	if allowed.Load()+denied.Load() != 100*100 {
		t.Fatalf("并发计数不完整")
	}
	// sync.Map + LoadOrStore 保证同 key 只有一个桶。
	if got := countBuckets(l); got != 3 {
		t.Errorf("并发后应有 3 个桶, got %d", got)
	}
}
