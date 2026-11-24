package ingest

import (
	"context"
	"errors"
	"sync"
)

// ErrClosed 表示 worker 已关闭，不再接受新任务。
var ErrClosed = errors.New("入库 worker 已关闭")

// task 是队列中的一项：携带回传结果的通道，让 Submit 能同步拿到结果。
type task struct {
	job     Job
	resultC chan Result
}

// Worker 是固定并发度的入库处理器。
//
// 并发模型：
//   - `tasks` 是有缓冲 channel，充当队列；缓冲满时 Submit 阻塞，形成背压，
//     避免任务无限堆积把内存撑爆；
//   - 固定数量的 goroutine 消费队列，并发度可控（不会因为突发流量起一万个
//     goroutine 去打数据库）；
//   - Close 通过 cancel 通知所有 worker 退出，并等待在途任务结束。
//
// 刻意不关闭 `tasks` channel：关闭后向它发送会 panic，而「提交」和「关闭」
// 在并发场景下无法靠 channel 关闭安全协调。这里用 ctx 广播代替，天然无竞态。
type Worker struct {
	tasks  chan task
	wg     sync.WaitGroup
	ctx    context.Context
	cancel context.CancelFunc
}

// New 启动 workers 个消费协程；queueSize 是队列缓冲长度。
func New(workers, queueSize int) *Worker {
	if workers <= 0 {
		workers = 4
	}
	if queueSize <= 0 {
		queueSize = workers * 4
	}

	ctx, cancel := context.WithCancel(context.Background())
	w := &Worker{
		tasks:  make(chan task, queueSize),
		ctx:    ctx,
		cancel: cancel,
	}

	w.wg.Add(workers)
	for i := 0; i < workers; i++ {
		go w.loop()
	}
	return w
}

// loop 是单个 worker 的主循环。
func (w *Worker) loop() {
	defer w.wg.Done()
	for {
		select {
		case <-w.ctx.Done():
			return
		case t := <-w.tasks:
			result := process(t.job)
			select {
			case t.resultC <- result:
			case <-w.ctx.Done():
				// 关闭中：调用方已通过 ctx 感知，无需回传
				return
			}
		}
	}
}

// process 执行实际的分块与指纹计算（CPU 密集，正是 worker pool 的价值）。
func process(job Job) Result {
	return Result{
		ID:          job.ID,
		Chunks:      Split(job.Content, DefaultMaxChunkRunes),
		Fingerprint: Fingerprint(job.Title, job.Content),
	}
}

// Submit 提交任务并等待结果。
//
// 三种失败路径都很明确：worker 已关闭、队列满且等待超时（ctx 取消）、
// 处理结果返回前 ctx 取消。
func (w *Worker) Submit(ctx context.Context, job Job) (Result, error) {
	resultC := make(chan Result, 1)

	select {
	case w.tasks <- task{job: job, resultC: resultC}:
	case <-w.ctx.Done():
		return Result{}, ErrClosed
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}

	select {
	case result := <-resultC:
		return result, nil
	case <-w.ctx.Done():
		return Result{}, ErrClosed
	case <-ctx.Done():
		return Result{}, ctx.Err()
	}
}

// Close 优雅关闭：停止接收新任务，等待在途任务处理完毕。
//
// 可重复调用：cancel 与 WaitGroup.Wait 都是幂等的。
func (w *Worker) Close() {
	w.cancel()
	w.wg.Wait()
}
