package ingest

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"
)

// newTestPipeline 统一构造测试用流水线（小队列，便于压出背压）。
func newTestPipeline(t *testing.T, cfg PipelineConfig) *Pipeline {
	t.Helper()
	p := NewPipeline(cfg)
	t.Cleanup(p.Close)
	return p
}

func mustJob(i int) Job {
	return Job{
		ID:      fmt.Sprintf("k-%d", i),
		Title:   fmt.Sprintf("标题 %d", i),
		Content: fmt.Sprintf("第 %d 份资料\n\n%s", i, strings.Repeat("并发分块", 80)),
	}
}

// TestPipelineSingleJob 验证单任务各阶段的输出与指纹。
func TestPipelineSingleJob(t *testing.T) {
	tests := []struct {
		name string
		job  Job
	}{
		{name: "单段", job: Job{ID: "k-1", Title: "标题", Content: "一段正文"}},
		{name: "多段", job: Job{ID: "k-2", Title: "标题", Content: "第一段\n\n第二段\n\n第三段"}},
		{name: "超长段", job: Job{ID: "k-3", Title: "标题", Content: strings.Repeat("字", 1300)}},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			p := newTestPipeline(t, PipelineConfig{Workers: 2, QueueSize: 4})

			res, err := p.Submit(context.Background(), tt.job)
			if err != nil {
				t.Fatalf("提交失败: %v", err)
			}

			wantChunks := Split(tt.job.Content, DefaultMaxChunkRunes)
			if !reflect.DeepEqual(res.Chunks, wantChunks) {
				t.Fatalf("分块不一致:\n got=%+v\nwant=%+v", res.Chunks, wantChunks)
			}
			wantFP := Fingerprint(tt.job.Title, tt.job.Content)
			if res.Fingerprint != wantFP {
				t.Fatalf("指纹不一致: got=%s want=%s", res.Fingerprint, wantFP)
			}
			if res.ID != tt.job.ID {
				t.Fatalf("ID 不一致: got=%s want=%s", res.ID, tt.job.ID)
			}

			if !p.ckpt.Reached(wantFP, StageParsed) {
				t.Fatal("checkpoint 未推进到 parsed")
			}
			if !p.ckpt.Reached(wantFP, StageChunked) {
				t.Fatal("checkpoint 未推进到 chunked")
			}
			if p.ckpt.Reached(wantFP, StageStored) {
				t.Fatal("未落库前不应到达 stored")
			}

			stats := p.Stats()
			if stats.Completed != 1 || stats.Failed != 0 || stats.Queued != 0 || stats.InFlight != 0 {
				t.Fatalf("统计不符合预期: %+v", stats)
			}

			// 落库阶段由调用方标记
			p.MarkStored(wantFP, "kid-"+tt.job.ID)
			stage, kid, ok := p.ckpt.Get(wantFP)
			if !ok || stage != StageStored || kid != "kid-"+tt.job.ID {
				t.Fatalf("MarkStored 后 checkpoint 不正确: stage=%s kid=%s ok=%v", stage, kid, ok)
			}
		})
	}
}

// TestPipelineConcurrentDifferentFingerprints 验证不同指纹并发提交。
func TestPipelineConcurrentDifferentFingerprints(t *testing.T) {
	const jobs = 32
	p := newTestPipeline(t, PipelineConfig{Workers: 4, QueueSize: 8})

	results := make([]Result, jobs)
	errs := make([]error, jobs)

	var wg sync.WaitGroup
	for i := 0; i < jobs; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			results[i], errs[i] = p.Submit(context.Background(), mustJob(i))
		}(i)
	}
	wg.Wait()

	for i, err := range errs {
		if err != nil {
			t.Fatalf("任务 %d 失败: %v", i, err)
		}
	}
	for i, res := range results {
		job := mustJob(i)
		if res.Fingerprint != Fingerprint(job.Title, job.Content) {
			t.Fatalf("任务 %d 指纹不一致", i)
		}
		if len(res.Chunks) == 0 {
			t.Fatalf("任务 %d 没有分块", i)
		}
		if !p.ckpt.Reached(res.Fingerprint, StageChunked) {
			t.Fatalf("任务 %d checkpoint 未推进", i)
		}
	}

	stats := p.Stats()
	if stats.Completed != jobs || stats.Failed != 0 || stats.Queued != 0 || stats.InFlight != 0 {
		t.Fatalf("统计不符合预期: %+v", stats)
	}
}

// TestPipelineBackpressure 验证队列极小时提交方阻塞但不会死锁/爆内存。
func TestPipelineBackpressure(t *testing.T) {
	const jobs = 48
	p := newTestPipeline(t, PipelineConfig{Workers: 1, QueueSize: 1})

	if got := cap(p.requests); got != 1 {
		t.Fatalf("请求队列容量应为 1，实际 %d", got)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	var wg sync.WaitGroup
	var mu sync.Mutex
	var failures []error

	for i := 0; i < jobs; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if _, err := p.Submit(ctx, mustJob(i)); err != nil {
				mu.Lock()
				failures = append(failures, fmt.Errorf("任务 %d: %w", i, err))
				mu.Unlock()
			}
		}(i)
	}

	done := make(chan struct{})
	go func() {
		wg.Wait()
		close(done)
	}()

	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("背压下提交被卡死（20s 未完成）")
	}

	if len(failures) != 0 {
		t.Fatalf("背压下出现失败: %v", failures)
	}

	stats := p.Stats()
	if stats.Completed != jobs || stats.Failed != 0 || stats.Queued != 0 || stats.InFlight != 0 {
		t.Fatalf("统计不符合预期: %+v", stats)
	}
}

// TestPipelineCancel 验证调用方 ctx 取消的两种时机。
func TestPipelineCancel(t *testing.T) {
	t.Run("提交前已取消", func(t *testing.T) {
		p := newTestPipeline(t, PipelineConfig{Workers: 2, QueueSize: 4})
		ctx, cancel := context.WithCancel(context.Background())
		cancel()

		_, err := p.Submit(ctx, mustJob(1))
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("期望 context.Canceled，实际 %v", err)
		}
	})

	t.Run("执行中取消", func(t *testing.T) {
		started := make(chan struct{})
		var once sync.Once
		cfg := PipelineConfig{Workers: 2, QueueSize: 4}
		cfg.beforeChunk = func(ctx context.Context) {
			once.Do(func() { close(started) })
			<-ctx.Done()
		}
		p := newTestPipeline(t, cfg)

		ctx, cancel := context.WithCancel(context.Background())
		errc := make(chan error, 1)
		go func() {
			_, err := p.Submit(ctx, mustJob(2))
			errc <- err
		}()

		select {
		case <-started:
		case <-time.After(2 * time.Second):
			t.Fatal("任务未进入分块阶段")
		}
		cancel()

		select {
		case err := <-errc:
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("期望 context.Canceled，实际 %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("取消后 Submit 未返回")
		}
	})
}

// TestPipelineLeaseLost 验证心跳续租失败会让任务以 ErrLeaseLost 结束。
func TestPipelineLeaseLost(t *testing.T) {
	started := make(chan struct{})
	cfg := PipelineConfig{
		Workers:   2,
		QueueSize: 4,
		LeaseTTL:  500 * time.Millisecond,
		Heartbeat: 2 * time.Millisecond,
	}
	cfg.leaseRefresh = func() error { return errors.New("存储层拒绝续租") }
	cfg.beforeChunk = func(ctx context.Context) {
		close(started)
		<-ctx.Done()
	}
	p := newTestPipeline(t, cfg)

	_, err := p.Submit(context.Background(), mustJob(3))
	if !errors.Is(err, ErrLeaseLost) {
		t.Fatalf("期望 ErrLeaseLost，实际 %v", err)
	}
	<-started

	stats := p.Stats()
	if stats.Failed != 1 || stats.Completed != 0 {
		t.Fatalf("统计不符合预期: %+v", stats)
	}
}

// TestPipelineStageTimeout 验证单阶段超时。
func TestPipelineStageTimeout(t *testing.T) {
	cfg := PipelineConfig{
		Workers:      1,
		QueueSize:    2,
		StageTimeout: 20 * time.Millisecond,
	}
	cfg.beforeChunk = func(ctx context.Context) { <-ctx.Done() }
	p := newTestPipeline(t, cfg)

	_, err := p.Submit(context.Background(), mustJob(4))
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("期望 DeadlineExceeded，实际 %v", err)
	}
}

// TestPipelineCloseWaitsInFlight 验证 Close 的关闭语义。
func TestPipelineCloseWaitsInFlight(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	cfg := PipelineConfig{Workers: 2, QueueSize: 4}
	cfg.beforeChunk = func(context.Context) {
		close(started)
		<-release
	}
	p := newTestPipeline(t, cfg)

	type submitResult struct {
		res Result
		err error
	}
	done := make(chan submitResult, 1)
	go func() {
		res, err := p.Submit(context.Background(), mustJob(5))
		done <- submitResult{res: res, err: err}
	}()

	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("任务未进入分块阶段")
	}

	closed := make(chan struct{})
	go func() {
		p.Close()
		close(closed)
	}()

	// Close 必须等在途任务跑完，而不是立刻返回。
	select {
	case <-closed:
		t.Fatal("Close 未等待在途任务")
	case <-time.After(80 * time.Millisecond):
	}

	close(release)

	select {
	case out := <-done:
		if out.err != nil {
			t.Fatalf("在途任务应该成功完成，实际 %v", out.err)
		}
		if len(out.res.Chunks) == 0 {
			t.Fatal("在途任务结果为空")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("在途任务未完成")
	}

	select {
	case <-closed:
	case <-time.After(2 * time.Second):
		t.Fatal("Close 未返回")
	}

	// 关闭后不再接单；重复 Close 安全。
	if _, err := p.Submit(context.Background(), mustJob(6)); !errors.Is(err, ErrClosed) {
		t.Fatalf("关闭后期望 ErrClosed，实际 %v", err)
	}
	p.Close()
}

// TestCheckpointSemantics 验证阶段推进的单调性与查询语义。
func TestCheckpointSemantics(t *testing.T) {
	var c Checkpoint

	if _, _, ok := c.Get("fp"); ok {
		t.Fatal("空 checkpoint 不应有记录")
	}
	if c.Reached("fp", StageParsed) {
		t.Fatal("空 checkpoint 不应到达任何阶段")
	}

	c.Mark("fp", StageParsed, "")
	if !c.Reached("fp", StageParsed) {
		t.Fatal("应已到达 parsed")
	}
	if c.Reached("fp", StageChunked) {
		t.Fatal("不应到达 chunked")
	}

	c.Mark("fp", StageStored, "k-1")
	stage, kid, ok := c.Get("fp")
	if !ok || stage != StageStored || kid != "k-1" {
		t.Fatalf("Get 结果不正确: stage=%s kid=%s ok=%v", stage, kid, ok)
	}

	// 迟到的低阶段标记不能倒退。
	c.Mark("fp", StageChunked, "")
	stage, kid, _ = c.Get("fp")
	if stage != StageStored || kid != "k-1" {
		t.Fatalf("阶段不应倒退: stage=%s kid=%s", stage, kid)
	}
	// 阶段回退但知识编号更新是允许的。
	c.Mark("fp", StageStored, "k-2")
	_, kid, _ = c.Get("fp")
	if kid != "k-2" {
		t.Fatalf("知识编号应被更新，实际 %s", kid)
	}

	if c.Reached("fp", StageChunked) != true || c.Reached("fp", StageStored) != true {
		t.Fatal("stored 应隐含到达更早阶段")
	}
}

// TestPipelineValidation 验证非法任务被拒绝且不影响后续使用。
func TestPipelineValidation(t *testing.T) {
	tests := []struct {
		name string
		job  Job
	}{
		{name: "空 ID", job: Job{ID: "", Content: "正文"}},
		{name: "空正文", job: Job{ID: "k-1", Content: ""}},
		{name: "纯空白正文", job: Job{ID: "k-2", Content: "  \n\t "}},
	}

	p := newTestPipeline(t, PipelineConfig{Workers: 2, QueueSize: 4})
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if _, err := p.Submit(context.Background(), tt.job); err == nil {
				t.Fatal("非法任务应返回错误")
			}
		})
	}

	// 非法提交不应污染统计；流水线仍可继续工作。
	stats := p.Stats()
	if stats.Completed != 0 || stats.Failed != 0 || stats.Queued != 0 {
		t.Fatalf("非法提交不应计数: %+v", stats)
	}
	if _, err := p.Submit(context.Background(), mustJob(7)); err != nil {
		t.Fatalf("校验失败后流水线应仍可用: %v", err)
	}
}

// TestPipelineSameFingerprintSerialized 验证同指纹并发提交串行化且不重复计算。
func TestPipelineSameFingerprintSerialized(t *testing.T) {
	const n = 16
	p := newTestPipeline(t, PipelineConfig{Workers: 4, QueueSize: 8})
	job := mustJob(42)

	start := make(chan struct{})
	results := make([]Result, n)
	errs := make([]error, n)

	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-start
			results[i], errs[i] = p.Submit(context.Background(), job)
		}(i)
	}
	close(start)
	wg.Wait()

	wantFP := Fingerprint(job.Title, job.Content)
	wantChunks := Split(job.Content, DefaultMaxChunkRunes)

	for i := 0; i < n; i++ {
		if errs[i] != nil {
			t.Fatalf("任务 %d 失败: %v", i, errs[i])
		}
		if results[i].Fingerprint != wantFP {
			t.Fatalf("任务 %d 指纹不一致", i)
		}
		if !reflect.DeepEqual(results[i].Chunks, wantChunks) {
			t.Fatalf("任务 %d 分块不一致", i)
		}
	}

	if got := p.computations.Load(); got != 1 {
		t.Fatalf("同指纹应只计算一次，实际 %d 次", got)
	}

	// 完成后再提交一次仍命中缓存。
	if _, err := p.Submit(context.Background(), job); err != nil {
		t.Fatalf("重复提交失败: %v", err)
	}
	if got := p.computations.Load(); got != 1 {
		t.Fatalf("重复提交不应重新计算，实际 %d 次", got)
	}

	stats := p.Stats()
	if stats.Completed != n+1 || stats.Failed != 0 {
		t.Fatalf("统计不符合预期: %+v", stats)
	}
}

// TestLeaseUnit 直接验证 Lease 的续租、过期、注入失败与幂等释放。
func TestLeaseUnit(t *testing.T) {
	t.Run("正常续租与幂等释放", func(t *testing.T) {
		now := time.Unix(1_700_000_000, 0)
		l := newLease(leaseOptions{
			fingerprint: "fp",
			ttl:         time.Second,
			heartbeat:   100 * time.Millisecond,
			now:         func() time.Time { return now },
		})

		if err := l.Refresh(); err != nil {
			t.Fatalf("正常续租失败: %v", err)
		}
		l.Release()
		l.Release() // 幂等

		if err := l.Refresh(); !errors.Is(err, ErrLeaseLost) {
			t.Fatalf("释放后续租应返回 ErrLeaseLost，实际 %v", err)
		}
		select {
		case <-l.Lost():
			t.Fatal("释放不是丢失，Lost 不应关闭")
		default:
		}
	})

	t.Run("过期", func(t *testing.T) {
		now := time.Unix(1_700_000_000, 0)
		l := newLease(leaseOptions{
			fingerprint: "fp",
			ttl:         time.Second,
			heartbeat:   100 * time.Millisecond,
			now:         func() time.Time { return now },
		})

		now = now.Add(2 * time.Second)
		if err := l.Refresh(); !errors.Is(err, ErrLeaseLost) {
			t.Fatalf("过期续租应返回 ErrLeaseLost，实际 %v", err)
		}
		select {
		case <-l.Lost():
		default:
			t.Fatal("过期后 Lost 应关闭")
		}
		l.Release()
	})

	t.Run("注入续租失败", func(t *testing.T) {
		lost := make(chan struct{}, 1)
		l := newLease(leaseOptions{
			fingerprint: "fp",
			ttl:         time.Second,
			heartbeat:   100 * time.Millisecond,
			refresh:     func() error { return errors.New("被接管") },
			onLost:      func() { lost <- struct{}{} },
		})

		if err := l.Refresh(); !errors.Is(err, ErrLeaseLost) {
			t.Fatalf("期望 ErrLeaseLost，实际 %v", err)
		}
		select {
		case <-lost:
		case <-time.After(time.Second):
			t.Fatal("onLost 未被调用")
		}
		select {
		case <-l.Lost():
		default:
			t.Fatal("丢失后 Lost 应关闭")
		}
		l.Release()
	})
}

// TestPipelineNoGoroutineLeak 验证 Close 之后后台协程能退出（尽力而为，带容忍度）。
func TestPipelineNoGoroutineLeak(t *testing.T) {
	baseline := runtime.NumGoroutine()

	func() {
		p := NewPipeline(PipelineConfig{Workers: 4, QueueSize: 8})
		var wg sync.WaitGroup
		for i := 0; i < 12; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				_, _ = p.Submit(context.Background(), mustJob(100+i))
			}(i)
		}
		wg.Wait()
		p.Close()
	}()

	deadline := time.Now().Add(2 * time.Second)
	for {
		runtime.GC()
		if runtime.NumGoroutine() <= baseline+2 {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("疑似协程泄漏：基线 %d，当前 %d", baseline, runtime.NumGoroutine())
		}
		time.Sleep(20 * time.Millisecond)
	}
}
