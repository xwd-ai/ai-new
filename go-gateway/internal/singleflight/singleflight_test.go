package singleflight

import (
	"context"
	"errors"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestConcurrentSameKeyExecutesOnce(t *testing.T) {
	g := New()
	var calls atomic.Int64
	block := make(chan struct{})

	const n = 10
	var wg sync.WaitGroup
	results := make([]string, n)
	errs := make([]error, n)

	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			v, err := Do(g, context.Background(), "doc:1", func(ctx context.Context) (string, error) {
				calls.Add(1)
				<-block // 让其余 9 个调用先挂在等待上
				return "payload", nil
			})
			results[i], errs[i] = v, err
		}(i)
	}

	// 等待所有 goroutine 都进入（最多 10 个）等待，再放行。
	// 轮询 calls 与等待者数量不可靠，用足够长的缓冲时间代替：
	time.Sleep(50 * time.Millisecond)
	close(block)
	wg.Wait()

	if calls.Load() != 1 {
		t.Errorf("fn 执行次数 = %d, want 1", calls.Load())
	}
	for i := 0; i < n; i++ {
		if errs[i] != nil {
			t.Errorf("第 %d 个调用出错: %v", i, errs[i])
		}
		if results[i] != "payload" {
			t.Errorf("第 %d 个结果 = %q, want payload", i, results[i])
		}
	}
}

func TestDifferentKeysExecuteIndependently(t *testing.T) {
	g := New()
	var calls atomic.Int64
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, _ = Do(g, context.Background(), "k"+strconv.Itoa(i), func(ctx context.Context) (int, error) {
				calls.Add(1)
				return i, nil
			})
		}(i)
	}
	wg.Wait()
	if calls.Load() != 8 {
		t.Errorf("不同 key 各自执行, 执行次数 = %d, want 8", calls.Load())
	}
}

func TestSharedError(t *testing.T) {
	g := New()
	wantErr := errors.New("upstream down")
	block := make(chan struct{})

	var wg sync.WaitGroup
	errs := make([]error, 5)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, err := Do(g, context.Background(), "k", func(ctx context.Context) (string, error) {
				<-block
				return "", wantErr
			})
			errs[i] = err
		}(i)
	}
	time.Sleep(30 * time.Millisecond)
	close(block)
	wg.Wait()

	for i, err := range errs {
		if !errors.Is(err, wantErr) {
			t.Errorf("第 %d 个调用错误 = %v, want %v", i, err, wantErr)
		}
	}
}

func TestContextCancelReturnsPromptly(t *testing.T) {
	g := New()
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() {
		_, err := Do(g, ctx, "slow", func(ctx context.Context) (string, error) {
			// fn 本身慢（且不理会 ctx），模拟慢上游。
			time.Sleep(2 * time.Second)
			return "late", nil
		})
		done <- err
	}()

	time.Sleep(30 * time.Millisecond) // 确保 Do 已挂上
	cancel()

	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Errorf("取消后错误 = %v, want context.Canceled", err)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("调用方 ctx 取消后未及时返回（被慢调用拖住）")
	}
}

func TestForgetDropsInFlightEntry(t *testing.T) {
	g := New()
	var calls atomic.Int64
	block := make(chan struct{})

	// 第一个调用在途（被 block 挡住）。
	go func() {
		_, _ = Do(g, context.Background(), "k", func(ctx context.Context) (int, error) {
			calls.Add(1)
			<-block
			return 1, nil
		})
	}()
	time.Sleep(30 * time.Millisecond)

	// Forget 后，新的同 key 调用不再与在途调用合并，
	// 应立刻开始自己的执行并很快返回。
	g.Forget("k")
	done := make(chan struct{})
	var v int
	go func() {
		defer close(done)
		v, _ = Do(g, context.Background(), "k", func(ctx context.Context) (int, error) {
			calls.Add(1)
			return 2, nil
		})
	}()
	select {
	case <-done:
		if v != 2 {
			t.Errorf("新执行结果 = %d, want 2", v)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("Forget 后新调用仍被在途调用阻塞")
	}

	close(block)
	if calls.Load() != 2 {
		t.Errorf("应各有一次执行, 执行次数 = %d, want 2", calls.Load())
	}
}

func TestSequentialSameKeyExecutesEachTime(t *testing.T) {
	// x/sync 只合并「在途」请求：调用完成后 entry 被移除，
	// 串行的第二次调用会重新执行（它不是结果缓存）。
	g := New()
	var calls atomic.Int64
	exec := func() {
		_, _ = Do(g, context.Background(), "k", func(ctx context.Context) (int, error) {
			calls.Add(1)
			return 0, nil
		})
	}
	exec()
	exec()
	if calls.Load() != 2 {
		t.Errorf("串行调用应各执行一次, 执行次数 = %d, want 2", calls.Load())
	}
}

func TestFnReceivesCancelableCtx(t *testing.T) {
	g := New()
	ctx, cancel := context.WithCancel(context.Background())

	done := make(chan error, 1)
	go func() {
		_, err := Do(g, ctx, "k", func(execCtx context.Context) (string, error) {
			<-execCtx.Done()
			return "", execCtx.Err()
		})
		done <- err
	}()

	time.Sleep(30 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		// fn 监听到了取消：发起者取消传播到执行方。
		if !errors.Is(err, context.Canceled) {
			t.Errorf("fn 应感知取消, err = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("fn 未感知到 ctx 取消")
	}
}

func TestNilGroupReturnsError(t *testing.T) {
	_, err := Do[struct{}](nil, context.Background(), "k", func(ctx context.Context) (struct{}, error) {
		return struct{}{}, nil
	})
	if err == nil {
		t.Error("nil Group 应返回错误而非 panic")
	}
}

func TestConcurrentMixedKeysRace(t *testing.T) {
	g := New()
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			key := "k" + strconv.Itoa(i%5)
			_, _ = Do(g, context.Background(), key, func(ctx context.Context) (int, error) {
				return i, nil
			})
		}(i)
	}
	wg.Wait()
}

func BenchmarkDo(b *testing.B) {
	g := New()
	ctx := context.Background()
	b.RunParallel(func(pb *testing.PB) {
		for pb.Next() {
			_, _ = Do(g, ctx, "bench-key", func(ctx context.Context) (int, error) {
				return 1, nil
			})
		}
	})
}
