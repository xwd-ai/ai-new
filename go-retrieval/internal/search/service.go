package search

import (
	"context"

	"golang.org/x/sync/errgroup"
)

// Embedder 把文本转成向量。
//
// 抽成接口而不是直接依赖模型 SDK：调用方可以注入任意实现，
// 测试里也能用桩替换，避免单测依赖网络与密钥。
type Embedder interface {
	Embed(ctx context.Context, text string) ([]float64, error)
}

// Searcher 编排双路召回：关键词与向量并行执行，再用 RRF 融合。
//
// Embedder 为 nil 时只走关键词路（相当于向量能力未配置），
// 与「向量不可用则退化为关键词检索」的预期行为一致。
type Searcher struct {
	Docs     []Document
	Embedder Embedder
	// Prefetch 是每路预取的条数，默认 topK*5：召回多取一些，给融合留出空间。
	Prefetch int
}

// Search 并行执行两路召回并融合，按相关性降序返回至多 topK 条。
//
// 并发要点：
//   - 关键词与向量两路互不依赖，用 errgroup 并行，总耗时约等于较慢那一路，
//     而不是两路之和；
//   - ctx 贯穿到 Embedder，上游超时或客户端断开能立即中止向量计算；
//   - 向量路失败只降级不报错 —— 检索有结果比整体失败更有价值。
func (s *Searcher) Search(ctx context.Context, query string, topK int) ([]Result, error) {
	if topK <= 0 || len(s.Docs) == 0 {
		return nil, nil
	}

	prefetch := s.Prefetch
	if prefetch <= 0 {
		prefetch = topK * 5
	}

	var keywordHits, vectorHits []Result

	g, gctx := errgroup.WithContext(ctx)

	g.Go(func() error {
		keywordHits = KeywordSearch(query, s.Docs, prefetch)
		return nil
	})

	g.Go(func() error {
		if s.Embedder == nil {
			return nil
		}
		vec, err := s.Embedder.Embed(gctx, query)
		if err != nil {
			// 向量路失败不阻断整体检索，退化为纯关键词结果
			return nil
		}
		vectorHits = VectorSearch(vec, s.Docs, prefetch)
		return nil
	})

	if err := g.Wait(); err != nil {
		return nil, err
	}

	// 只有一路有结果时直接返回，省掉无意义的融合
	if len(vectorHits) == 0 {
		if len(keywordHits) > topK {
			keywordHits = keywordHits[:topK]
		}
		return keywordHits, nil
	}
	if len(keywordHits) == 0 {
		if len(vectorHits) > topK {
			vectorHits = vectorHits[:topK]
		}
		return vectorHits, nil
	}

	return FuseRRF(keywordHits, vectorHits, topK, DefaultRRFK), nil
}
