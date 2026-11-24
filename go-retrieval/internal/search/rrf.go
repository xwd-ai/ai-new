package search

import (
	"math"
	"sort"
)

// DefaultRRFK 是 RRF 的平滑常数。
//
// 取值越大，各路「第一名」的优势越弱、结果越平均；60 是文献里的常用值。
const DefaultRRFK = 60.0

// FuseRRF 用倒数排名融合（Reciprocal Rank Fusion）合并两路召回结果。
//
// 只用**名次**不用分值：余弦相似度与 BM25 分数量纲完全不同，直接相加会让
// 分数尺度大的那一路压倒另一路。RRF 把「排第几」作为唯一信号，
// 天然规避了量纲问题。
//
// 融合分再按理论最大值 2/(k+1) 归一化到 [0,1]，使混合结果与单路结果可比。
func FuseRRF(keyword, vector []Result, topK int, k float64) []Result {
	if topK <= 0 {
		return nil
	}
	if k <= 0 {
		k = DefaultRRFK
	}

	score := make(map[string]float64, len(keyword)+len(vector))
	meta := make(map[string]Result, len(keyword)+len(vector))
	inKeyword := make(map[string]bool, len(keyword))
	inVector := make(map[string]bool, len(vector))

	for rank, r := range keyword {
		score[r.ID] += 1.0 / (k + float64(rank+1))
		meta[r.ID] = r
		inKeyword[r.ID] = true
	}
	for rank, r := range vector {
		score[r.ID] += 1.0 / (k + float64(rank+1))
		if _, ok := meta[r.ID]; !ok {
			meta[r.ID] = r
		}
		inVector[r.ID] = true
	}

	// 两路都排第一时融合分最大：1/(k+1) + 1/(k+1)
	maxScore := 2.0 / (k + 1)
	results := make([]Result, 0, len(score))
	for id, s := range score {
		r := meta[id]
		r.Score = math.Min(1, s/maxScore)

		switch {
		case inKeyword[id] && inVector[id]:
			r.MatchType = MatchHybrid
		case inVector[id]:
			r.MatchType = MatchVector
		default:
			r.MatchType = MatchKeyword
		}
		results = append(results, r)
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
