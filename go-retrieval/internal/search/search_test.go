package search

import (
	"reflect"
	"testing"
)

func TestTokenize(t *testing.T) {
	tests := []struct {
		name  string
		input string
		want  []string
	}{
		{"中文按单字", "导出失败", []string{"导", "出", "失", "败"}},
		{"英文整词小写", "Export FAILS", []string{"export", "fails"}},
		{"数字与符号", "v2.0-beta", []string{"v2", "0", "beta"}},
		{"中英混合", "PDF 导出", []string{"pdf", "导", "出"}},
		{"空串", "", nil},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := tokenize(tt.input)
			if !reflect.DeepEqual(got, tt.want) {
				t.Fatalf("tokenize(%q) = %v, want %v", tt.input, got, tt.want)
			}
		})
	}
}

// fixture 是三篇题材不同的文档，关键词检索用它验证排序与截断。
func fixture() []Document {
	return []Document{
		{ID: "k-export", Title: "导出功能失败", Content: "导出 PDF 时点击无反应", Keywords: []string{"导出", "PDF"}},
		{ID: "k-refund", Title: "退款政策", Content: "七天无理由退款流程", Keywords: []string{"退款"}},
		{ID: "k-activate", Title: "激活码使用", Content: "在设置页输入激活码", Keywords: []string{"激活"}},
	}
}

func TestKeywordSearch(t *testing.T) {
	tests := []struct {
		name      string
		query     string
		docs      []Document
		topK      int
		wantIDs   []string
		wantEmpty bool
	}{
		{name: "命中标题关键词", query: "导出失败", docs: fixture(), topK: 3, wantIDs: []string{"k-export"}},
		{name: "命中无关题材", query: "退款", docs: fixture(), topK: 3, wantIDs: []string{"k-refund"}},
		{name: "topK 截断", query: "激活", docs: fixture(), topK: 1, wantIDs: []string{"k-activate"}},
		{name: "无命中返回空", query: "区块链", docs: fixture(), topK: 3, wantEmpty: true},
		{name: "空查询返回空", query: "", docs: fixture(), topK: 3, wantEmpty: true},
		{name: "空文档集返回空", query: "导出", docs: nil, topK: 3, wantEmpty: true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := KeywordSearch(tt.query, tt.docs, tt.topK)
			if tt.wantEmpty {
				if len(got) != 0 {
					t.Fatalf("期望空结果，实际 %v", got)
				}
				return
			}
			if len(got) == 0 {
				t.Fatalf("期望命中 %v，实际为空", tt.wantIDs)
			}
			if got[0].ID != tt.wantIDs[0] {
				t.Fatalf("首位期望 %s，实际 %s（完整: %v）", tt.wantIDs[0], got[0].ID, got)
			}
			// 归一化后最高分应为 1
			if got[0].Score > 1 || got[0].Score <= 0 {
				t.Fatalf("分数应在 (0,1]，实际 %v", got[0].Score)
			}
			if got[0].MatchType != MatchKeyword {
				t.Fatalf("MatchType 期望 %s，实际 %s", MatchKeyword, got[0].MatchType)
			}
			if len(got) > tt.topK {
				t.Fatalf("结果数 %d 超过 topK %d", len(got), tt.topK)
			}
		})
	}
}

func TestVectorSearch(t *testing.T) {
	docs := []Document{
		{ID: "k-a", Title: "A", Vector: []float64{1, 0, 0}},
		{ID: "k-b", Title: "B", Vector: []float64{0, 1, 0}},
		{ID: "k-c", Title: "C", Vector: []float64{0.9, 0.1, 0}},
	}

	tests := []struct {
		name    string
		query   []float64
		topK    int
		wantIDs []string
	}{
		{name: "最相近排第一", query: []float64{1, 0, 0}, topK: 2, wantIDs: []string{"k-a", "k-c"}},
		{name: "正交不命中", query: []float64{0, 0, 1}, topK: 3, wantIDs: nil},
		{name: "维度不一致按 0 处理", query: []float64{1, 0}, topK: 3, wantIDs: nil},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := VectorSearch(tt.query, docs, tt.topK)
			if len(tt.wantIDs) == 0 {
				if len(got) != 0 {
					t.Fatalf("期望空结果，实际 %v", got)
				}
				return
			}
			if len(got) < len(tt.wantIDs) {
				t.Fatalf("结果不足，期望 %v 开头，实际 %v", tt.wantIDs, got)
			}
			for i, want := range tt.wantIDs {
				if got[i].ID != want {
					t.Fatalf("第 %d 位期望 %s，实际 %s（完整: %v）", i, want, got[i].ID, got)
				}
			}
		})
	}
}

func TestFuseRRF(t *testing.T) {
	tests := []struct {
		name       string
		keyword    []Result
		vector     []Result
		topK       int
		wantFirst  string
		wantMatch  string
		wantLength int
	}{
		{
			name:       "两路都命中为 hybrid 且排前",
			keyword:    []Result{{ID: "k-a"}, {ID: "k-b"}},
			vector:     []Result{{ID: "k-a"}, {ID: "k-c"}},
			topK:       3,
			wantFirst:  "k-a",
			wantMatch:  MatchHybrid,
			wantLength: 3,
		},
		{
			name:       "只有关键词路",
			keyword:    []Result{{ID: "k-b"}},
			vector:     nil,
			topK:       2,
			wantFirst:  "k-b",
			wantMatch:  MatchKeyword,
			wantLength: 1,
		},
		{
			name:       "只有向量路",
			keyword:    nil,
			vector:     []Result{{ID: "k-c"}},
			topK:       2,
			wantFirst:  "k-c",
			wantMatch:  MatchVector,
			wantLength: 1,
		},
		{
			name:       "两路皆空",
			topK:       3,
			wantLength: 0,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := FuseRRF(tt.keyword, tt.vector, tt.topK, 0)
			if len(got) != tt.wantLength {
				t.Fatalf("结果数期望 %d，实际 %d（%v）", tt.wantLength, len(got), got)
			}
			if tt.wantLength == 0 {
				return
			}
			if got[0].ID != tt.wantFirst {
				t.Fatalf("首位期望 %s，实际 %s", tt.wantFirst, got[0].ID)
			}
			if got[0].MatchType != tt.wantMatch {
				t.Fatalf("MatchType 期望 %s，实际 %s", tt.wantMatch, got[0].MatchType)
			}
			if got[0].Score > 1 || got[0].Score <= 0 {
				t.Fatalf("归一化分数应在 (0,1]，实际 %v", got[0].Score)
			}
		})
	}
}

// TestConcurrentReadSafety 验证检索函数只读文档集合，可被并发调用。
// 配合 go test -race 可捕获数据竞争。
func TestConcurrentReadSafety(t *testing.T) {
	docs := fixture()
	const workers = 16

	done := make(chan struct{}, workers)
	for i := 0; i < workers; i++ {
		go func() {
			defer func() { done <- struct{}{} }()
			KeywordSearch("导出失败", docs, 3)
			VectorSearch([]float64{1, 0}, docs, 3)
			FuseRRF(
				[]Result{{ID: "k-a"}},
				[]Result{{ID: "k-b"}},
				2, 0,
			)
		}()
	}
	for i := 0; i < workers; i++ {
		<-done
	}
}
