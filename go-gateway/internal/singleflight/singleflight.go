// Package singleflight 合并并发相同的请求，避免缓存击穿。
//
// 为什么需要这个包：网关聚合上游接口时，同一个 key 的并发请求
// （例如同一篇文档的热点查询）会导致上游被打出 N 份完全相同的工作；
// 合并成一次执行、结果共享给所有等待者，可以把上游负载从 N 降到 1。
//
// 设计取舍：
//   - 直接封装 golang.org/x/sync/singleflight：它的去重语义
//     （含 Forget、错误共享、panic 传播）久经验证，自研容易踩并发坑；
//   - 在其上补两件事：泛型 Do（免掉调用方的类型断言）和
//     context 取消——x/sync 的 Do 不感知 ctx，慢调用会把已取消的
//     调用方一起拖住，用 DoChan + select ctx.Done 解决；
//   - 共享结果契约：所有等待者拿到的是同一个 T 值（非拷贝），
//     调用方不得修改返回值，需要可变结果请自行拷贝。
package singleflight

import (
	"context"
	"errors"

	"golang.org/x/sync/singleflight"
)

// Group 封装 singleflight.Group，零值不可直接使用，请通过 New 创建。
type Group struct {
	// g 是标准库 singleflight 分组。Group 的方法是并发安全的，
	// 因此 Group 本身无需额外锁。
	g singleflight.Group
}

// New 创建一个 Group。
func New() *Group {
	return &Group{}
}

// Do 执行并去重：同一 key 的并发调用只有一个真正执行 fn，
// 其余共享其结果。返回值与错误对所有调用方相同。
//
// ctx 语义：ctx 取消时本调用尽快返回 ctx.Err()（慢调用
// 会被放弃等待，但不会中断 fn 本身——fn 应自行监听传入的 ctx）。
// 注意：fn 收到的 ctx 是「本 key 首个发起者的 ctx 的取消信号」，
// 该调用方取消时执行方会收到取消；如果这不满足需求，
// 调用方应在 fn 内部再派生自己的 ctx。
func Do[T any](g *Group, ctx context.Context, key string, fn func(context.Context) (T, error)) (T, error) {
	var zero T
	if g == nil {
		return zero, errGroupNil
	}
	ch := g.g.DoChan(key, func() (any, error) {
		// 用发起者 ctx 派生执行 ctx：调用方放弃（取消）时，
		// 正在执行的 fn 也能收到信号尽快收尾。
		execCtx, cancel := context.WithCancel(ctx)
		defer cancel()
		return fn(execCtx)
	})

	select {
	case <-ctx.Done():
		// 调用方已放弃：不再等共享结果。
		// 注：x/sync 只合并「在途」请求，完成后 entry 会被移除，
		// 不缓存结果；但共享调用可能在没有等待者时仍在执行，
		// Forget 可让后续同 key 调用立刻开启新的执行。
		return zero, ctx.Err()
	case res := <-ch:
		if res.Err != nil {
			return zero, res.Err
		}
		v, _ := res.Val.(T)
		return v, nil
	}
}

// Forget 丢弃 key 的在途调用记录：执行中的调用继续跑完（其等待者
// 仍拿到它的结果），但之后的 Do 会开启一次新的执行。
// 用于上游数据已失效时，主动切断新调用与旧在途结果的合并。
func (g *Group) Forget(key string) {
	g.g.Forget(key)
}

// errGroupNil 在 Group 为 nil 时返回，避免空指针 panic。
var errGroupNil = errors.New("singleflight: Group 为 nil")
