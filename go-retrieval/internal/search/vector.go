package search

import "math"

// VectorSearch 用余弦相似度对文档排序，只保留正相似度的结果。
//
// 维度不一致的文档向量会被视为不可比（相似度按 0 处理），
// 这样即便调用方混入了不同维度的向量，也不会污染排序结果。
func VectorSearch(queryVector []float64, docs []Document, topK int) []Result {
	if len(queryVector) == 0 || len(docs) == 0 || topK <= 0 {
		return nil
	}

	scores := make([]float64, len(docs))
	for i, doc := range docs {
		sim := cosine(queryVector, doc.Vector)
		if sim > 0 {
			scores[i] = math.Min(1, sim)
		}
	}
	return collect(docs, scores, MatchVector, topK)
}

// cosine 计算余弦相似度；任一向量为零向量或维度不一致时返回 0。
func cosine(a, b []float64) float64 {
	if len(a) == 0 || len(a) != len(b) {
		return 0
	}

	var dot, normA, normB float64
	for i := range a {
		dot += a[i] * b[i]
		normA += a[i] * a[i]
		normB += b[i] * b[i]
	}
	if normA == 0 || normB == 0 {
		return 0
	}
	return dot / (math.Sqrt(normA) * math.Sqrt(normB))
}
