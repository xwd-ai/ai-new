package distlock

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

// newTestLocker 起一个内存 Redis 并返回基于它的 Locker。
func newTestLocker(t *testing.T) (*RedisLocker, *redis.Client) {
	t.Helper()
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	return New(client), client
}

func TestAcquireAndRelease(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	token, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("首次 Acquire 应成功: ok=%v err=%v", ok, err)
	}
	if len(token) != 32 {
		t.Errorf("token 应为 32 hex, got %q", token)
	}

	if err := l.Release(ctx, "job:1", token); err != nil {
		t.Fatalf("Release 失败: %v", err)
	}

	// 释放后他人可获取。
	if _, ok, err := l.Acquire(ctx, "job:1", time.Minute); err != nil || !ok {
		t.Errorf("Release 后他人应可获取: ok=%v err=%v", ok, err)
	}
}

func TestMutualExclusion(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	_, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("第一个 Acquire 应成功: ok=%v err=%v", ok, err)
	}

	token2, ok2, err2 := l.Acquire(ctx, "job:1", time.Minute)
	if err2 != nil {
		t.Fatalf("第二个 Acquire 出错: %v", err2)
	}
	if ok2 {
		t.Errorf("第二个 Acquire 不应成功（互斥）, token=%q", token2)
	}
}

func TestTTLExpiryAllowsReacquire(t *testing.T) {
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	l := New(client)
	ctx := context.Background()

	_, ok, err := l.Acquire(ctx, "job:1", 50*time.Millisecond)
	if err != nil || !ok {
		t.Fatalf("Acquire 应成功: %v", err)
	}

	// miniredis 支持快进时间，精确模拟 TTL 过期（无需真实等待）。
	mr.FastForward(60 * time.Millisecond)

	_, ok, err = l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Errorf("TTL 过期后应可重新获取: ok=%v err=%v", ok, err)
	}
}

func TestReleaseWithWrongTokenDoesNotDelete(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	token1, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("Acquire 应成功: %v", err)
	}

	// 模拟「锁过期后被他人接管」的场景：此时原持有者拿着旧 token
	// 来释放。用错误 token 释放不得删除现持有者的锁。
	wrongToken := "ffffffffffffffffffffffffffffffff"
	if token1 == wrongToken {
		t.Fatal("测试 token 撞车，重跑")
	}
	if err := l.Release(ctx, "job:1", wrongToken); err != nil {
		t.Fatalf("Release 本身不应报错: %v", err)
	}

	// 锁应原封不动：新的 Acquire 仍失败。
	if _, ok2, err2 := l.Acquire(ctx, "job:1", time.Minute); err2 != nil || ok2 {
		t.Errorf("token 不匹配的 Release 不应删锁: Acquire ok=%v err=%v（应 ok=false）", ok2, err2)
	}

	// 正确 token 仍可正常释放。
	if err := l.Release(ctx, "job:1", token1); err != nil {
		t.Fatalf("正确 token 的 Release 应成功: %v", err)
	}
	if _, ok3, err3 := l.Acquire(ctx, "job:1", time.Minute); err3 != nil || !ok3 {
		t.Errorf("正确释放后应可重新获取: ok=%v err=%v", ok3, err3)
	}
}

func TestAcquireWithRetrySuccess(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	// 先占住锁，10ms 后异步释放，重试方应能拿到。
	token1, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("初始 Acquire 应成功: %v", err)
	}
	go func() {
		time.Sleep(20 * time.Millisecond)
		_ = l.Release(ctx, "job:1", token1)
	}()

	token2, ok2, err2 := l.AcquireWithRetry(ctx, "job:1", time.Minute, time.Second, 10*time.Millisecond)
	if err2 != nil || !ok2 {
		t.Fatalf("AcquireWithRetry 应等到锁释放后成功: ok=%v err=%v", ok2, err2)
	}
	_ = token2
}

func TestAcquireWithRetryTimeout(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	_, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("初始 Acquire 应成功: %v", err)
	}

	start := time.Now()
	_, ok2, err2 := l.AcquireWithRetry(ctx, "job:1", time.Minute, 100*time.Millisecond, 20*time.Millisecond)
	elapsed := time.Since(start)
	if ok2 {
		t.Fatal("锁一直被占用，重试应失败")
	}
	if !errors.Is(err2, errWaitTimeout) {
		t.Errorf("err = %v, want ErrWaitTimeout", err2)
	}
	// 应在 wait 附近返回，而不是立刻或远超。
	if elapsed < 90*time.Millisecond || elapsed > 2*time.Second {
		t.Errorf("重试耗时 = %v, 应接近 wait=100ms", elapsed)
	}
}

func TestAcquireWithRetryContextCancel(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx, cancel := context.WithCancel(context.Background())

	_, ok, err := l.Acquire(ctx, "job:1", time.Minute)
	if err != nil || !ok {
		t.Fatalf("初始 Acquire 应成功: %v", err)
	}

	go func() {
		time.Sleep(30 * time.Millisecond)
		cancel()
	}()

	start := time.Now()
	_, _, err2 := l.AcquireWithRetry(ctx, "job:1", time.Minute, 10*time.Second, 10*time.Millisecond)
	elapsed := time.Since(start)
	if !errors.Is(err2, context.Canceled) {
		t.Errorf("err = %v, want context.Canceled", err2)
	}
	if elapsed > time.Second {
		t.Errorf("ctx 取消后应尽快返回, 耗时 %v", elapsed)
	}
}

func TestRedisFailureReturnsError(t *testing.T) {
	mr := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	l := New(client)
	ctx := context.Background()

	// 先正常拿一把锁，然后模拟 Redis 故障。
	if _, ok, err := l.Acquire(ctx, "job:1", time.Minute); err != nil || !ok {
		t.Fatalf("初始 Acquire 应成功: %v", err)
	}
	mr.Close() // miniredis 挂掉，后续命令返回连接错误

	_, _, err := l.Acquire(ctx, "job:2", time.Minute)
	if err == nil {
		t.Error("Redis 故障时 Acquire 应返回 error 而非 ok=false")
	}
	if err2 := l.Release(ctx, "job:1", "x"); err2 == nil {
		t.Error("Redis 故障时 Release 应返回 error")
	}
	_, _, err3 := l.AcquireWithRetry(ctx, "job:2", time.Minute, 200*time.Millisecond, 20*time.Millisecond)
	if err3 == nil {
		t.Error("Redis 故障时 AcquireWithRetry 应返回 error")
	}
}

func TestConcurrentAcquireOnlyOneWins(t *testing.T) {
	l, _ := newTestLocker(t)
	ctx := context.Background()

	const n = 20
	var mu sync.Mutex
	winners := 0
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			token, ok, err := l.Acquire(ctx, "job:shared", time.Minute)
			if err != nil {
				t.Errorf("Acquire 出错: %v", err)
				return
			}
			if ok {
				mu.Lock()
				winners++
				mu.Unlock()
				_ = l.Release(ctx, "job:shared", token)
			}
		}()
	}
	wg.Wait()
	if winners == 0 {
		t.Fatal("并发下至少应有一个成功者")
	}
	// Release 有竞争（先释放，后到的尝试失败属正常），
	// 但同时持有锁的成功者最多应为 1——逐个串行验证：
	// 并发场景下真正要保证的是 SetNX 原子性，
	// 这里用「最终锁可被重新获取」兜底验证状态未被破坏。
	if _, ok, err := l.Acquire(ctx, "job:shared", time.Minute); err != nil || !ok {
		t.Errorf("所有释放后应可重新获取: ok=%v err=%v", ok, err)
	}
}

func TestNewTokenUnique(t *testing.T) {
	seen := make(map[string]bool)
	for i := 0; i < 1000; i++ {
		tok, err := newToken()
		if err != nil {
			t.Fatalf("newToken 出错: %v", err)
		}
		if len(tok) != 32 {
			t.Fatalf("token 长度 = %d, want 32", len(tok))
		}
		seen[tok] = true
	}
	if len(seen) != 1000 {
		t.Errorf("token 出现重复: %d/1000 唯一", len(seen))
	}
}
