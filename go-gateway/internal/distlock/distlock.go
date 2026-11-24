// Package distlock 提供基于 Redis 的分布式锁，与仓库中 Python 版
// distlock.py 保持同一协议（token + Lua 比对删除），使 Go 网关与
// Python 服务可以互相竞争/释放同一把锁。
//
// 为什么需要这个包：网关多实例部署时，本地 mutex 无法约束跨进程的
// 互斥（例如「同一任务只允许一个实例执行」），必须把锁放到所有
// 实例共享的存储上。选 Redis 是因为它是链路里已有的组件，
// SET NX PX 一条命令即具备原子性，无需额外引入 etcd/ZooKeeper。
//
// 设计取舍：
//   - token 用 crypto/rand 32 hex：锁的持有者身份必须全局唯一且
//     不可预测，这是「比对后删除」能防止误删他人锁的前提；
//   - Release 必须 Lua 原子完成 get+比对+del：拆成两条命令的话，
//     get 通过到 del 之间锁可能恰好过期并被他人接管，del 就会
//     误删别人的锁——这是分布式锁最常见的坑，所以即使牺牲一点
//     可读性也要用 Lua；
//   - 不在这里做看门狗自动续期：调用方最清楚临界区多长，
//     显式传 TTL 比隐式续期更可预期（自动续期 + 进程崩溃 =
//     锁永远不释放的另一种事故）。
package distlock

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"time"

	"github.com/redis/go-redis/v9"
)

// releaseScript 原子地比对 token 并删除锁。
// KEYS[1]=锁 key, ARGV[1]=持有者 token。
// 返回 1 表示删除成功（锁确为本持有者），0 表示 token 不匹配或已过期。
var releaseScript = redis.NewScript(`
if redis.call('get', KEYS[1]) == ARGV[1] then
	return redis.call('del', KEYS[1])
else
	return 0
end
`)

// Locker 是分布式锁的最小接口。抽象成接口便于业务侧用假实现
// 做单元测试，不必每个测试都起 miniredis。
type Locker interface {
	// Acquire 尝试加锁，成功返回持有的 token 与 ok=true；
	// 未获取（他人持有）返回空 token 与 ok=false；err 仅在
	// Redis 通信故障时非 nil。
	Acquire(ctx context.Context, key string, ttl time.Duration) (token string, ok bool, err error)
	// Release 只有 token 匹配才删除（Lua 比对后删除）。
	// 释放不存在的锁不视为错误。
	Release(ctx context.Context, key, token string) error
}

// RedisLocker 基于 go-redis 实现 Locker。
type RedisLocker struct {
	client redis.UniversalClient
}

// 编译期断言 RedisLocker 实现 Locker。
var _ Locker = (*RedisLocker)(nil)

// New 创建 RedisLocker。client 由调用方管理生命周期（连接池复用）。
func New(client redis.UniversalClient) *RedisLocker {
	return &RedisLocker{client: client}
}

// newToken 生成 16 字节的随机 token（32 hex）。
// 用满 16 字节（128 位）而非 8 字节：锁 token 需要在所有实例、
// 所有时间维度上不重复，碰撞后果是误删他人锁，安全边际要给足。
func newToken() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

// Acquire 用 SET key token NX PX ttl 原子加锁。
func (l *RedisLocker) Acquire(ctx context.Context, key string, ttl time.Duration) (string, bool, error) {
	token, err := newToken()
	if err != nil {
		return "", false, err
	}
	// SetNX + TTL 必须一条命令完成；先 SETNX 再 EXPIRE 的话，
	// 两步之间进程崩溃会留下一把永不过期的死锁。
	ok, err := l.client.SetNX(ctx, key, token, ttl).Result()
	if err != nil {
		return "", false, err
	}
	if !ok {
		return "", false, nil // 他人持有，不是错误
	}
	return token, true, nil
}

// Release 用 Lua 比对 token 后删除，避免误删已被他人接管的锁。
func (l *RedisLocker) Release(ctx context.Context, key, token string) error {
	_, err := releaseScript.Run(ctx, l.client, []string{key}, token).Result()
	return err
}

// errWaitTimeout 表示 AcquireWithRetry 在 wait 时限内未抢到锁。
var errWaitTimeout = errors.New("distlock: 等待锁超时")

// ErrWaitTimeout 暴露给调用方判断（errors.Is）。
func ErrWaitTimeout() error { return errWaitTimeout }

// AcquireWithRetry 在 wait 时限内以 interval 轮询抢锁，
// 适合任务调度等「锁被占用时稍等重试」的场景。
// 优先响应 ctx 取消/超时，其次响应 wait 总时限。
// 拿不到锁返回 ErrWaitTimeout，而不是无限自旋。
func (l *RedisLocker) AcquireWithRetry(ctx context.Context, key string, ttl, wait, interval time.Duration) (string, bool, error) {
	if interval <= 0 {
		interval = 100 * time.Millisecond
	}
	deadline := time.Now().Add(wait)
	for {
		token, ok, err := l.Acquire(ctx, key, ttl)
		if err != nil {
			return "", false, err
		}
		if ok {
			return token, true, nil
		}
		// 距离下次尝试的等待时间：不能超过 deadline。
		next := interval
		if remain := time.Until(deadline); remain < next {
			next = remain
		}
		if next <= 0 {
			return "", false, errWaitTimeout
		}
		timer := time.NewTimer(next)
		select {
		case <-ctx.Done():
			timer.Stop()
			return "", false, ctx.Err()
		case <-timer.C:
			// 到点后继续循环；若已越过 deadline，
			// 下一轮 remain<=0 会返回超时。
		}
		if time.Now().After(deadline) {
			return "", false, errWaitTimeout
		}
	}
}
