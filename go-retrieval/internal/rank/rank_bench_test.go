package rank

import (
	"fmt"
	"strings"
	"testing"
)

func benchItems(n int) []Item {
	items := make([]Item, n)
	for i := 0; i < n; i++ {
		items[i] = Item{
			"id":       fmt.Sprintf("doc-%04d", i),
			"title":    fmt.Sprintf("知识条目标题%d混合文本", i),
			"kind":     []string{"rule", "faq", "manual"}[i%3],
			"content":  strings.Repeat("退款政策与发货流程说明，48小时内可处理。Refund policy applies within 7 days. ", 8),
			"keywords": []string{"退款", "发货", "policy"},
			"score":    1.0 - float64(i)/float64(n),
		}
	}
	return items
}

func BenchmarkRank(b *testing.B) {
	results := benchItems(200)
	scores := make([]float64, len(results))
	for i := range scores {
		scores[i] = 1.0 - float64(i)/float64(len(results))
	}
	opts := Options{TopK: 5, Config: DefaultConfig()}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out, _ := Rank("退款政策是什么", results, scores, opts)
		if len(out) == 0 {
			b.Fatal("empty result")
		}
	}
}

func BenchmarkRankNoProvider(b *testing.B) {
	results := benchItems(200)
	opts := Options{TopK: 5, Config: DefaultConfig()}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out, _ := Rank("退款政策是什么", results, nil, opts)
		if len(out) == 0 {
			b.Fatal("empty result")
		}
	}
}

func BenchmarkSelectMMR(b *testing.B) {
	results := benchItems(200)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out := SelectMMR(results, 10, 0.7)
		if len(out) != 10 {
			b.Fatal("unexpected result size")
		}
	}
}

func BenchmarkTokenize(b *testing.B) {
	text := strings.Repeat("退款政策与发货流程说明，48小时内可处理。Refund policy applies within 7 days. ", 20)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if tokens := Tokenize(text); len(tokens) == 0 {
			b.Fatal("empty tokens")
		}
	}
}

func BenchmarkFuseRRFWeighted(b *testing.B) {
	vector := benchItems(100)
	keyword := benchItems(100)
	for i := range keyword {
		keyword[i] = deepCopyItem(keyword[i])
		keyword[i]["id"] = fmt.Sprintf("kw-%04d", i)
	}
	cfg := DefaultConfig()
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		out := FuseRRFWeighted(vector, keyword, 20, cfg)
		if len(out) != 20 {
			b.Fatal("unexpected result size")
		}
	}
}
