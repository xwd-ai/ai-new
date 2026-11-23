// Package search 实现关键词与向量双路召回，并用 RRF 融合两路结果。
//
// 本包不依赖 HTTP 与数据库：输入是纯数据结构，输出是排序后的结果，
// 因此既能在服务里使用，也能被测试直接驱动，便于独立验证算法行为。
package search

// Document 是检索的最小单元。
//
// Vector 由调用方预先算好并传入：本服务不持有模型客户端，
// 只负责排序，这样向量化方式（模型、维度）完全由调用方决定。
type Document struct {
	ID       string    `json:"id"`
	Title    string    `json:"title"`
	Content  string    `json:"content"`
	Keywords []string  `json:"keywords,omitempty"`
	Vector   []float64 `json:"-"`
}

// Result 是一条检索结果。
//
// MatchType 标注命中来源：keyword / vector / hybrid，
// 调用方据此可以区分「精确词命中」与「语义相近」。
type Result struct {
	ID        string  `json:"id"`
	Title     string  `json:"title"`
	Content   string  `json:"content"`
	Score     float64 `json:"score"`
	MatchType string  `json:"match_type"`
}

// MatchType 取值。
const (
	MatchKeyword = "keyword"
	MatchVector  = "vector"
	MatchHybrid  = "hybrid"
)
