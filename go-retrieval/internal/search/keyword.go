package search

import (
	"math"
	"sort"
)

// BM25 参数：k1 控制词频饱和速度，b 控制文档长度归一化强度。
const (
	bm25K1 = 1.5
	bm25B  = 0.75
)

// KeywordSearch 用 BM25 对文档打分，返回降序的 topK 结果。
//
// 分数会归一化到 [0,1]：不同查询的原始 BM25 分不可比，
// 归一化后既能跨查询比较，也为与向量分融合提供统一量纲。
//
// 并发安全：只读取传入切片，不修改文档集合，可被多个 goroutine 同时调用。
func KeywordSearch(query string, docs []Document, topK int) []Result {
	queryTokens := tokenize(query)
	if len(queryTokens) == 0 || len(docs) == 0 || topK <= 0 {
		return nil
	}

	docTokens := make([][]string, len(docs))
	totalLen := 0
	for i, doc := range docs {
		docTokens[i] = documentTokens(doc)
		totalLen += len(docTokens[i])
	}
	avgLen := float64(totalLen) / float64(len(docs))
	if avgLen == 0 {
		return nil
	}

	// 只统计查询词的文档频次，DF 表不需要覆盖全部词元
	wanted := make(map[string]struct{}, len(queryTokens))
	for _, token := range queryTokens {
		wanted[token] = struct{}{}
	}
	df := make(map[string]int, len(wanted))
	for _, tokens := range docTokens {
		seen := make(map[string]struct{}, len(tokens))
		for _, token := range tokens {
			if _, dup := seen[token]; dup {
				continue
			}
			seen[token] = struct{}{}
			if _, ok := wanted[token]; ok {
				df[token]++
			}
		}
	}

	docCount := float64(len(docs))
	scores := make([]float64, len(docs))
	for i, tokens := range docTokens {
		tf := make(map[string]int, len(tokens))
		for _, token := range tokens {
			tf[token]++
		}

		score := 0.0
		for token := range wanted {
			freq := float64(tf[token])
			if freq == 0 {
				continue
			}
			// 平滑 IDF，避免高频词权重为负
			idf := math.Log(1 + (docCount-float64(df[token])+0.5)/(float64(df[token])+0.5))
			denom := freq + bm25K1*(1-bm25B+bm25B*float64(len(tokens))/avgLen)
			score += idf * freq * (bm25K1 + 1) / denom
		}
		scores[i] = score
	}

	normalize(scores)
	return collect(docs, scores, MatchKeyword, topK)
}

// normalize 把分数缩放到 [0,1]，以列表最大值为基准；全零时保持全零。
func normalize(scores []float64) {
	maxScore := 0.0
	for _, s := range scores {
		if s > maxScore {
			maxScore = s
		}
	}
	if maxScore <= 0 {
		return
	}
	for i := range scores {
		scores[i] /= maxScore
	}
}

// collect 把「文档 + 分数」整理成降序结果，丢弃零分并截断到 topK。
//
// 同分时按 ID 升序：保证同一输入多次调用结果顺序稳定，便于测试与对比。
func collect(docs []Document, scores []float64, matchType string, topK int) []Result {
	results := make([]Result, 0, len(docs))
	for i, doc := range docs {
		if scores[i] <= 0 {
			continue
		}
		results = append(results, Result{
			ID:        doc.ID,
			Title:     doc.Title,
			Content:   doc.Content,
			Score:     scores[i],
			MatchType: matchType,
		})
	}

	sort.Slice(results, func(i, j int) bool {
		if results[i].Score != results[j].Score {
			return results[i].Score > results[j].Score
		}
		return results[i].ID < results[j].ID
	})

	if len(results) > topK {
		results = results[:topK]
	}
	return results
}
