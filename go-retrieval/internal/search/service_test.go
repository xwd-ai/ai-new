package search

import (
	"context"
	"errors"
	"testing"
	"time"
)

// stubEmbedder 是测试用的固定向量实现，避免依赖真实模型。
type stubEmbedder struct {
	vec []float64
	err error
}

func (s stubEmbedder) Embed(_ context.Context, _ string) ([]float64, error) {
	return s.vec, s.err
}

// slowEmbedder 模拟慢速模型调用，且**响应 context 取消** ——
// 真实 Embedder 应如此，否则上游超时无法终止下游计算。
type slowEmbedder struct{ delay time.Duration }

func (s slowEmbedder) Embed(ctx context.Context, _ string) ([]float64, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(s.delay):
		return []float64{1, 0}, nil
	}
}

func serviceFixture() []Document {
	return []Document{
		{ID: "k-export", Title: "导出功能失败", Content: "导出 PDF 无反应", Keywords: []string{"导出"}, Vector: []float64{1, 0}},
		{ID: "k-refund", Title: "退款政策", Content: "七天无理由退款", Keywords: []string{"退款"}, Vector: []float64{0, 1}},
	}
}

func TestSearcher_Search(t *testing.T) {
	tests := []struct {
		name      string
		embedder  Embedder
		query     string
		topK      int
		wantFirst string
	}{
		{
			name:      "两路融合命中",
			embedder:  stubEmbedder{vec: []float64{1, 0}},
			query:     "导出失败",
			topK:      2,
			wantFirst: "k-export",
		},
		{
			name:      "无向量能力时退化为关键词",
			embedder:  nil,
			query:     "退款",
			topK:      2,
			wantFirst: "k-refund",
		},
		{
			name:      "向量路报错时降级不失败",
			embedder:  stubEmbedder{err: errors.New("模型不可用")},
			query:     "导出",
			topK:      2,
			wantFirst: "k-export",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			s := &Searcher{Docs: serviceFixture(), Embedder: tt.embedder}
			got, err := s.Search(context.Background(), tt.query, tt.topK)
			if err != nil {
				t.Fatalf("不应返回错误: %v", err)
			}
			if len(got) == 0 {
				t.Fatalf("期望命中结果，实际为空")
			}
			if got[0].ID != tt.wantFirst {
				t.Fatalf("首位期望 %s，实际 %s（完整: %v）", tt.wantFirst, got[0].ID, got)
			}
			if len(got) > tt.topK {
				t.Fatalf("结果数 %d 超过 topK %d", len(got), tt.topK)
			}
		})
	}
}

// TestSearcher_Hybrid 验证两路都命中时被标记为 hybrid，说明两路都参与了融合。
func TestSearcher_Hybrid(t *testing.T) {
	s := &Searcher{Docs: serviceFixture(), Embedder: stubEmbedder{vec: []float64{1, 0}}}

	got, err := s.Search(context.Background(), "导出", 2)
	if err != nil {
		t.Fatalf("检索失败: %v", err)
	}
	if len(got) == 0 {
		t.Fatal("期望有结果")
	}
	if got[0].ID != "k-export" || got[0].MatchType != MatchHybrid {
		t.Fatalf("期望 k-export 且 MatchType=hybrid，实际 %s/%s", got[0].ID, got[0].MatchType)
	}
}

// TestSearcher_ContextCancel 验证上游取消能终止检索。
func TestSearcher_ContextCancel(t *testing.T) {
	s := &Searcher{Docs: serviceFixture(), Embedder: slowEmbedder{delay: time.Second}}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()

	start := time.Now()
	if _, err := s.Search(ctx, "导出", 2); err != nil {
		t.Fatalf("检索不应报错: %v", err)
	}
	if elapsed := time.Since(start); elapsed > 500*time.Millisecond {
		t.Fatalf("超时未生效，耗时 %v", elapsed)
	}
}
