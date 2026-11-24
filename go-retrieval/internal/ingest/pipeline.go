package ingest

// 本文件把「知识入库」在 Go 侧组织成一条有界并发流水线。
//
// 与 worker.go 的关系：
//   - worker.go 只提供「固定 worker pool + 有界队列 + Submit 回传结果」的原语，
//     它不关心任务从哪来、失败如何恢复、重复提交如何协调；
//   - Pipeline 在它之上补齐幂等（指纹串行化 + 结果缓存）、阶段检查点、
//     执行权租约（心跳续租 / 丢失即中止）、优雅关闭与可观测计数。
//
// 这里刻意不直接翻译 Python 的 threading 版本：
//   - 用「有界 channel」而不是无界 list 做队列，提交方在队列满时阻塞，
//     把压力清晰地传回调用方，内存占用有硬上限；
//   - 用「每个任务一个 context + context.AfterFunc」把调用方断开、租约丢失、
//     流水线关闭三类信号统一收敛到 cancel，而不是散落的布尔标志；
//   - 所有长期存活的 goroutine（阶段 worker、心跳、生命周期哨兵）挂在同一个
//     errgroup 上，Close 时统一 cancel + Wait，不靠 sleep/轮询判断退出。
//
// 明确不负责的部分：真正的落库仍由 Python 侧完成。这里的 Lease 管理的是
// 「本次进程内流水线的执行权」，不是 SQLite 租约表；Checkpoint 也只是内存
// journal，用于展示阶段推进与幂等判定，不做持久化。

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sync/errgroup"
)

// Stage 是入库流水线阶段，取值对齐 Python ingest.py 的 STAGE_* 常量。
type Stage string

const (
	StageParsed  Stage = "parsed"
	StageChunked Stage = "chunked"
	StageStored  Stage = "stored"
)

// stageRank 给出阶段的单调顺序，用于判断「是否已经到达过某阶段」。
// 未知阶段返回 0，永远不会让已记录的阶段倒退。
func stageRank(s Stage) int {
	switch s {
	case StageParsed:
		return 1
	case StageChunked:
		return 2
	case StageStored:
		return 3
	default:
		return 0
	}
}

// ErrLeaseLost 表示任务执行权已经失效（过期或被他人接管）。
//
// 与 Python 的 IngestLeaseLostError 对齐：拿到它就不要继续写任何结果，
// 交由调用方重新提交。
var ErrLeaseLost = errors.New("入库租约已失效")

const (
	defaultPipelineWorkers = 4
	defaultLeaseTTL        = 30 * time.Second // 对齐 Python LEASE_SECONDS
	defaultHeartbeat       = 10 * time.Second // 对齐 Python HEARTBEAT_SECONDS

	// 结果缓存上限：同一指纹重复提交时直接复用上次分块结果，避免重复 CPU 计算。
	// 缓存是「加速」而非「正确性」依赖，淘汰后最坏只是重算一次。
	defaultResultCacheSize = 1024
)

// ---------------------------------------------------------------------------
// Checkpoint
// ---------------------------------------------------------------------------

type checkpointEntry struct {
	stage       Stage
	knowledgeID string
}

// Checkpoint 记录每个指纹到达的阶段（内存 + RWMutex），
// 对应 Python 的 journal / checkpoint。
//
// 语义：
//   - Mark 只前进不后退：已经 stored 的记录不会被一次迟到的 chunked 覆盖；
//   - knowledgeID 非空时总是更新（落库完成后补上真实编号）；
//   - Reached 是「至少到达」判断，便于恢复时决定从哪一步继续；
//   - 零值可用。
//
// 注意：它是进程内 journal，不持久化；进程重启后的恢复仍以 Python 的
// SQLite journal 为准。
type Checkpoint struct {
	mu sync.RWMutex
	m  map[string]*checkpointEntry
}

// Mark 记录指纹到达了某个阶段。
func (c *Checkpoint) Mark(fingerprint string, stage Stage, knowledgeID string) {
	if c == nil || fingerprint == "" {
		return
	}
	c.mu.Lock()
	if c.m == nil {
		c.m = make(map[string]*checkpointEntry)
	}
	e := c.m[fingerprint]
	if e == nil {
		e = &checkpointEntry{}
		c.m[fingerprint] = e
	}
	if stageRank(stage) > stageRank(e.stage) {
		e.stage = stage
	}
	if knowledgeID != "" {
		e.knowledgeID = knowledgeID
	}
	c.mu.Unlock()
}

// Reached 判断指纹是否「至少」到达过 stage。
func (c *Checkpoint) Reached(fingerprint string, stage Stage) bool {
	if c == nil {
		return false
	}
	c.mu.RLock()
	e := c.m[fingerprint]
	c.mu.RUnlock()
	return e != nil && stageRank(e.stage) >= stageRank(stage)
}

// Get 返回指纹当前阶段与知识编号。
func (c *Checkpoint) Get(fingerprint string) (stage Stage, knowledgeID string, ok bool) {
	if c == nil {
		return "", "", false
	}
	c.mu.RLock()
	e := c.m[fingerprint]
	c.mu.RUnlock()
	if e == nil {
		return "", "", false
	}
	return e.stage, e.knowledgeID, true
}

// ---------------------------------------------------------------------------
// Lease
// ---------------------------------------------------------------------------

type leaseState int

const (
	leaseActive leaseState = iota
	leaseLost
	leaseReleased
)

// Lease 是一次进程内执行的租约。
//
// 它用 time.Ticker 周期续租；续租失败（过期 / 外部注入失败 / 已被接管）时
// 关闭 lost 通道并通过 onLost 取消任务 context。Release 幂等，通常 defer 调用。
//
// 这里的租约不碰数据库：Python 侧写 SQLite 租约表是跨进程协调，本租约只用于
// 让「一次 Pipeline 执行」在失去执行权时立刻停手，避免旧执行者继续算/写。
type Lease struct {
	fingerprint string
	ttl         time.Duration
	heartbeat   time.Duration

	now       func() time.Time
	refreshFn func() error
	onLost    func()

	lostCh chan struct{}
	doneCh chan struct{}

	lostOnce sync.Once
	doneOnce sync.Once

	mu        sync.Mutex
	state     leaseState
	expiresAt time.Time
}

type leaseOptions struct {
	fingerprint string
	ttl         time.Duration
	heartbeat   time.Duration
	now         func() time.Time
	refresh     func() error
	onLost      func()
}

func newLease(o leaseOptions) *Lease {
	if o.ttl <= 0 {
		o.ttl = defaultLeaseTTL
	}
	if o.heartbeat <= 0 {
		o.heartbeat = defaultHeartbeat
	}
	// 心跳必须快于 TTL，否则第一次续租前就过期了。
	if o.heartbeat >= o.ttl {
		o.heartbeat = o.ttl / 3
		if o.heartbeat <= 0 {
			o.heartbeat = o.ttl
		}
	}
	if o.now == nil {
		o.now = time.Now
	}
	return &Lease{
		fingerprint: o.fingerprint,
		ttl:         o.ttl,
		heartbeat:   o.heartbeat,
		now:         o.now,
		refreshFn:   o.refresh,
		onLost:      o.onLost,
		lostCh:      make(chan struct{}),
		doneCh:      make(chan struct{}),
		expiresAt:   o.now().Add(o.ttl),
	}
}

// Lost 返回一个在租约丢失时关闭的通道。
func (l *Lease) Lost() <-chan struct{} {
	return l.lostCh
}

// Refresh 续租。任何失败都会把租约标记为丢失并返回 ErrLeaseLost。
//
// refreshFn 是测试注入点：真实实现里这里是对存储层做一次「我仍持有」的条件更新，
// 测试用它模拟「已被他人接管」。
func (l *Lease) Refresh() error {
	l.mu.Lock()
	switch l.state {
	case leaseLost:
		l.mu.Unlock()
		return ErrLeaseLost
	case leaseReleased:
		l.mu.Unlock()
		return fmt.Errorf("%w: 租约已释放", ErrLeaseLost)
	}
	if l.now().After(l.expiresAt) {
		l.mu.Unlock()
		l.markLost()
		return fmt.Errorf("%w: 租约已过期", ErrLeaseLost)
	}
	l.mu.Unlock()

	if l.refreshFn != nil {
		if err := l.refreshFn(); err != nil {
			l.markLost()
			return fmt.Errorf("%w: %v", ErrLeaseLost, err)
		}
	}

	l.mu.Lock()
	if l.state != leaseActive {
		l.mu.Unlock()
		return ErrLeaseLost
	}
	l.expiresAt = l.now().Add(l.ttl)
	l.mu.Unlock()
	return nil
}

// Release 释放租约。幂等，可安全 defer 调用；释放后心跳协程会自然退出。
func (l *Lease) Release() {
	l.mu.Lock()
	if l.state == leaseReleased {
		l.mu.Unlock()
		return
	}
	l.state = leaseReleased
	l.mu.Unlock()

	l.doneOnce.Do(func() { close(l.doneCh) })
}

// markLost 把租约置为丢失并触发一次回调（只触发一次）。
func (l *Lease) markLost() {
	l.mu.Lock()
	if l.state != leaseActive {
		l.mu.Unlock()
		return
	}
	l.state = leaseLost
	l.mu.Unlock()

	l.lostOnce.Do(func() {
		close(l.lostCh)
		if l.onLost != nil {
			l.onLost()
		}
	})
}

// ---------------------------------------------------------------------------
// Stats / Config
// ---------------------------------------------------------------------------

// Stats 是流水线的可观测计数快照。所有字段通过 atomic 读取，Stats() 无锁。
type Stats struct {
	// Queued 已接受但尚未被阶段 worker 取走的任务数（含在 channel 里排队的）。
	Queued int64
	// InFlight 正在执行 parse/lease/chunk 的任务数。
	InFlight int64
	// Completed 成功返回结果的任务数。
	Completed int64
	// Failed 失败返回的任务数（含 ctx 取消、租约丢失、关闭中断）。
	Failed int64
	// Retried 内部自动重试次数。当前策略不做自动重试：
	// 租约丢失/关闭都立即失败并交由调用方重试，避免在内存里放大故障。
	Retried int64
}

// PipelineConfig 是流水线配置。
type PipelineConfig struct {
	Workers      int           // 分块阶段并发度，透传给 Worker
	QueueSize    int           // 队列缓冲，形成背压
	LeaseTTL     time.Duration // 租约有效期（默认 30s，对齐 Python LEASE_SECONDS）
	Heartbeat    time.Duration // 续租间隔（默认 10s，对齐 Python HEARTBEAT_SECONDS）
	StageTimeout time.Duration // 单任务单阶段超时（默认 0 表示不设）

	// 以下为测试钩子，未导出：外部调用方不应依赖。
	leaseRefresh func() error          // 注入续租失败
	beforeChunk  func(context.Context) // 在真正分块前插入一段可控逻辑
}

// ---------------------------------------------------------------------------
// Pipeline
// ---------------------------------------------------------------------------

type pipelineRequest struct {
	job       Job
	fp        string
	callerCtx context.Context
	out       chan outcome
}

type outcome struct {
	res Result
	err error
}

// Pipeline 是一条并发入库流水线。
//
// 结构：
//
//	Submit ──► requests(有界 channel) ──► stage worker ×N
//	                                         │  同指纹串行化
//	                                         │  查结果缓存
//	                                         │  获取 Lease + 心跳
//	                                         ▼
//	                                    Worker.Submit（复用现有 worker pool）
//
// 背压来自 requests 的容量：队列满时 Submit 阻塞，且随时可被调用方 ctx 取消。
// 阶段 worker、生命周期哨兵与心跳都挂在 errgroup 上，Close 统一 cancel + Wait；
// Worker 自带的池协程不在本组内（read-only 依赖），由 worker.Close 负责收口。
type Pipeline struct {
	cfg    PipelineConfig
	worker *Worker
	gate   *keyedLocks
	cache  *resultCache
	ckpt   *Checkpoint

	baseCtx context.Context
	cancel  context.CancelFunc
	g       *errgroup.Group

	requests chan *pipelineRequest

	mu     sync.Mutex
	closed bool
	drain  sync.WaitGroup

	closeOnce sync.Once

	queued    atomic.Int64
	inFlight  atomic.Int64
	completed atomic.Int64
	failed    atomic.Int64
	retried   atomic.Int64

	// computations 是真正进入分块计算的次数（测试用来验证同指纹不重复计算）。
	computations atomic.Int64
}

// NewPipeline 启动流水线。
func NewPipeline(cfg PipelineConfig) *Pipeline {
	if cfg.Workers <= 0 {
		cfg.Workers = defaultPipelineWorkers
	}
	if cfg.QueueSize <= 0 {
		cfg.QueueSize = cfg.Workers * 4
	}
	if cfg.LeaseTTL <= 0 {
		cfg.LeaseTTL = defaultLeaseTTL
	}
	if cfg.Heartbeat <= 0 {
		cfg.Heartbeat = defaultHeartbeat
	}
	if cfg.Heartbeat >= cfg.LeaseTTL {
		cfg.Heartbeat = cfg.LeaseTTL / 3
		if cfg.Heartbeat <= 0 {
			cfg.Heartbeat = cfg.LeaseTTL
		}
	}

	baseCtx, cancel := context.WithCancel(context.Background())
	p := &Pipeline{
		cfg:      cfg,
		worker:   New(cfg.Workers, cfg.QueueSize),
		gate:     newKeyedLocks(),
		cache:    newResultCache(defaultResultCacheSize),
		ckpt:     &Checkpoint{},
		baseCtx:  baseCtx,
		cancel:   cancel,
		g:        &errgroup.Group{},
		requests: make(chan *pipelineRequest, cfg.QueueSize),
	}

	// 哨兵协程让 errgroup 计数在 Close 前始终 > 0：阶段 worker 可以安全地
	// 在处理任务时动态 g.Go 出心跳协程，不会出现「计数归零后被 Wait 提前返回」。
	p.g.Go(func() error {
		<-baseCtx.Done()
		return nil
	})
	for i := 0; i < cfg.Workers; i++ {
		p.g.Go(p.stageLoop)
	}
	return p
}

// Submit 提交一个任务并等待结果。
//
// 返回值的错误语义：
//   - 队列满且调用方 ctx 取消：ctx.Err()
//   - 调用方 ctx 取消 / 单阶段超时：context.Canceled / context.DeadlineExceeded
//   - 租约丢失：ErrLeaseLost
//   - 流水线已关闭：ErrClosed
//   - 任务本身非法（ID/正文为空）：普通 error，不 panic
func (p *Pipeline) Submit(ctx context.Context, job Job) (Result, error) {
	if ctx == nil {
		ctx = context.Background()
	}

	// 接单判定与 drain.Add 必须在同一把锁里完成：
	// Close 只有在置位 closed 之后才 Wait，保证 Add 永远发生在 Wait 之前。
	p.mu.Lock()
	if p.closed {
		p.mu.Unlock()
		return Result{}, ErrClosed
	}
	p.drain.Add(1)
	p.mu.Unlock()
	defer p.drain.Done()

	if err := validateJob(job); err != nil {
		return Result{}, err
	}
	// 已取消的调用方不必再进队列：直接给出确定的 ctx 错误，
	// 避免「取消信号与入队赛跑」导致偶发成功。
	if err := ctx.Err(); err != nil {
		return Result{}, err
	}

	req := &pipelineRequest{
		job:       job,
		fp:        Fingerprint(job.Title, job.Content),
		callerCtx: ctx,
		out:       make(chan outcome, 1),
	}
	// parse 在 Submit 内同步完成（纯内存操作、无 I/O）：校验通过即代表已解析。
	// 先记号，即使后面排队被取消或落库失败，也能看出这份内容曾经到过 parsed。
	p.ckpt.Mark(req.fp, StageParsed, "")

	p.queued.Add(1)
	select {
	case p.requests <- req:
	case <-ctx.Done():
		p.queued.Add(-1)
		return Result{}, ctx.Err()
	case <-p.baseCtx.Done():
		p.queued.Add(-1)
		return Result{}, ErrClosed
	}

	// 只等阶段回传：每个被接受的任务都保证恰好一次回传（out 有 1 个缓冲，
	// 即使调用方提前返回也不会让 worker 阻塞）。调用方取消会通过逐任务 ctx
	// 传进 stage，stage 会尽快带着 ctx 错误回传。
	select {
	case out := <-req.out:
		return out.res, out.err
	case <-p.baseCtx.Done():
		// 不可达：Close 会先等 drain 再 cancel；保留这一分支是为了在
		// 未来改动 Close 顺序时也不会永久阻塞提交方。
		return Result{}, ErrClosed
	}
}

// stageLoop 是分块阶段的 worker：从有界队列取任务，处理完把结果放回请求。
func (p *Pipeline) stageLoop() error {
	for {
		select {
		case <-p.baseCtx.Done():
			return nil
		case req := <-p.requests:
			p.queued.Add(-1)
			p.inFlight.Add(1)

			res, err := p.processOne(req)

			p.inFlight.Add(-1)
			if err != nil {
				p.failed.Add(1)
			} else {
				p.completed.Add(1)
			}
			req.out <- outcome{res: res, err: err}
		}
	}
}

// processOne 完成单个任务的 parse 后处理：同指纹串行化 → 缓存 → 租约 → 分块。
func (p *Pipeline) processOne(req *pipelineRequest) (Result, error) {
	// 逐任务 context：派生自流水线 ctx，并监听调用方 ctx。
	// 用 AfterFunc 而不是再起一个 goroutine 做 select，减少每任务一个常驻协程。
	taskCtx, cancelTask := context.WithCancelCause(p.baseCtx)
	// AfterFunc 对已取消的 ctx 是异步触发的；先同步检查一次，
	// 保证「取消发生在进入 stage 之前」也能立刻停下，而不是先跑一段再停。
	if err := req.callerCtx.Err(); err != nil {
		cancelTask(err)
	}
	stopWatch := context.AfterFunc(req.callerCtx, func() {
		cancelTask(context.Cause(req.callerCtx))
	})
	defer func() {
		stopWatch()
		cancelTask(nil) // 收尾：让 ctx 关联的回调/计时器尽快释放
	}()

	if p.cfg.StageTimeout > 0 {
		var cancelTimeout context.CancelFunc
		taskCtx, cancelTimeout = context.WithTimeout(taskCtx, p.cfg.StageTimeout)
		defer cancelTimeout()
	}

	// 同指纹串行化：并发的重复提交在这里排队，而不是同时算两遍。
	// 选择「等待」而非直接返回 busy/ErrLeaseLost：HTTP 场景下重复上传同一份
	// 资料是常态，让后来者等前一次算完并命中缓存，比让客户端重试更友好。
	// 代价是等待者会暂时占住一个阶段 worker 槽位，可能挤压其它指纹的吞吐；
	// 但占用上限就是 Workers，不会无限增长，属于可接受的公平性取舍。
	release, err := p.gate.acquire(taskCtx, req.fp)
	if err != nil {
		return Result{}, taskError(taskCtx, err)
	}
	defer release()

	if err := taskCtx.Err(); err != nil {
		return Result{}, taskError(taskCtx, err)
	}

	// 幂等快路径：同指纹已经算过，直接返回同一份结果（Chunks 做副本，
	// 防止调用方修改切片污染缓存）。
	if cached, ok := p.cache.get(req.fp); ok {
		p.ckpt.Mark(req.fp, StageChunked, "")
		return cached, nil
	}

	lease := newLease(leaseOptions{
		fingerprint: req.fp,
		ttl:         p.cfg.LeaseTTL,
		heartbeat:   p.cfg.Heartbeat,
		refresh:     p.cfg.leaseRefresh,
		onLost:      func() { cancelTask(ErrLeaseLost) },
	})
	p.g.Go(func() error { return p.superviseLease(lease) })
	defer lease.Release()

	if hook := p.cfg.beforeChunk; hook != nil {
		hook(taskCtx)
	}
	if err := taskCtx.Err(); err != nil {
		return Result{}, taskError(taskCtx, err)
	}

	p.computations.Add(1)
	res, err := p.worker.Submit(taskCtx, req.job)
	if err != nil {
		return Result{}, taskError(taskCtx, err)
	}

	p.cache.put(req.fp, res)
	p.ckpt.Mark(req.fp, StageChunked, "")
	return res, nil
}

// superviseLease 是单个租约的心跳协程：按 Heartbeat 周期续租，失败即退出。
func (p *Pipeline) superviseLease(l *Lease) error {
	ticker := time.NewTicker(l.heartbeat)
	defer ticker.Stop()

	for {
		select {
		case <-l.doneCh:
			return nil
		case <-p.baseCtx.Done():
			return nil
		case <-ticker.C:
			if err := l.Refresh(); err != nil {
				return nil // Refresh 内部已 markLost 并取消任务 ctx
			}
		}
	}
}

// MarkStored 由调用方（HTTP handler / Python 落库完成后）触发，
// 记录指纹已到达 stored 阶段。
func (p *Pipeline) MarkStored(fingerprint, knowledgeID string) {
	p.ckpt.Mark(fingerprint, StageStored, knowledgeID)
}

// Stats 返回计数快照，无锁。
func (p *Pipeline) Stats() Stats {
	return Stats{
		Queued:    p.queued.Load(),
		InFlight:  p.inFlight.Load(),
		Completed: p.completed.Load(),
		Failed:    p.failed.Load(),
		Retried:   p.retried.Load(),
	}
}

// Close 优雅关闭：停止接单 → 等在途 → 停 worker pool → cancel → Wait。
//
// 可重复调用（sync.Once，并发调用会一起阻塞到关闭完成）。
//
// 取舍：这里先等 drain 再 cancel，而不是一上来就 cancel 所有 ctx。原因是要满足
// 「Close 后已在途的任务跑完再返回」；如果立刻 cancel，在途任务会被误伤成失败。
// 代价是 Close 的耗时取决于在途任务；生产上应由调用方给 Close 加外层超时。
func (p *Pipeline) Close() {
	p.closeOnce.Do(func() {
		p.mu.Lock()
		p.closed = true
		p.mu.Unlock()

		// 已接受的任务全部结束后，worker pool 才真正空闲，此时关闭不会截断任务。
		p.drain.Wait()

		p.worker.Close()
		p.cancel()
		_ = p.g.Wait()
	})
}

// ---------------------------------------------------------------------------
// 校验 / 错误归因
// ---------------------------------------------------------------------------

func validateJob(job Job) error {
	if strings.TrimSpace(job.ID) == "" {
		return errors.New("ingest: job.ID 不能为空")
	}
	if strings.TrimSpace(job.Content) == "" {
		return errors.New("ingest: job.Content 不能为空")
	}
	return nil
}

// taskError 优先返回 context 的 cause：租约丢失、调用方取消、阶段超时
// 都会被 context 记录成明确的错误，而不是笼统的 context.Canceled。
func taskError(ctx context.Context, fallback error) error {
	if cause := context.Cause(ctx); cause != nil {
		return cause
	}
	return fallback
}

// ---------------------------------------------------------------------------
// 同指纹串行化
// ---------------------------------------------------------------------------

type keyedLock struct {
	sem  chan struct{}
	refs int
}

type keyedLocks struct {
	mu sync.Mutex
	m  map[string]*keyedLock
}

func newKeyedLocks() *keyedLocks {
	return &keyedLocks{m: make(map[string]*keyedLock)}
}

// acquire 获取某个指纹的执行权；等待过程可被 ctx 取消。
// 返回的 release 幂等（内部 once），调用方通常 defer 它。
func (k *keyedLocks) acquire(ctx context.Context, key string) (func(), error) {
	k.mu.Lock()
	e := k.m[key]
	if e == nil {
		e = &keyedLock{sem: make(chan struct{}, 1)}
		k.m[key] = e
	}
	e.refs++
	k.mu.Unlock()

	select {
	case e.sem <- struct{}{}:
		var once sync.Once
		return func() {
			once.Do(func() {
				<-e.sem
				k.mu.Lock()
				e.refs--
				if e.refs == 0 {
					delete(k.m, key)
				}
				k.mu.Unlock()
			})
		}, nil
	case <-ctx.Done():
		k.mu.Lock()
		e.refs--
		if e.refs == 0 {
			delete(k.m, key)
		}
		k.mu.Unlock()
		return nil, ctx.Err()
	}
}

// ---------------------------------------------------------------------------
// 结果缓存
// ---------------------------------------------------------------------------

type cacheItem struct {
	key string
	res Result
}

// resultCache 是带容量上限的 LRU：同指纹重复提交直接复用结果。
//
// 为什么要缓存：字符串指纹相同意味着标题+正文规范化后完全一致，分块结果必然
// 相同；重复算一遍纯属浪费 CPU。淘汰策略是 LRU，命中率不是关键指标，
// 「不重复计算当前热点」才是。
type resultCache struct {
	mu   sync.Mutex
	max  int
	ll   *list.List
	item map[string]*list.Element
}

func newResultCache(max int) *resultCache {
	if max <= 0 {
		max = defaultResultCacheSize
	}
	return &resultCache{
		max:  max,
		ll:   list.New(),
		item: make(map[string]*list.Element, max),
	}
}

func (c *resultCache) get(key string) (Result, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()

	e, ok := c.item[key]
	if !ok {
		return Result{}, false
	}
	c.ll.MoveToFront(e)
	return cloneResult(e.Value.(*cacheItem).res), true
}

func (c *resultCache) put(key string, res Result) {
	clone := cloneResult(res)

	c.mu.Lock()
	defer c.mu.Unlock()

	if e, ok := c.item[key]; ok {
		e.Value.(*cacheItem).res = clone
		c.ll.MoveToFront(e)
		return
	}
	e := c.ll.PushFront(&cacheItem{key: key, res: clone})
	c.item[key] = e
	for c.ll.Len() > c.max {
		back := c.ll.Back()
		if back == nil {
			break
		}
		it := back.Value.(*cacheItem)
		delete(c.item, it.key)
		c.ll.Remove(back)
	}
}

func cloneResult(r Result) Result {
	if r.Chunks != nil {
		r.Chunks = append([]Chunk(nil), r.Chunks...)
	}
	return r
}
