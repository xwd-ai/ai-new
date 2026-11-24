package rank

import "math"

// SelectMMR 用最大边际相关（MMR）从候选中选出既相关又互不重复的若干条。
//
// 贪心选择：每轮挑出 λ×相关性 − (1−λ)×已选条目中的最大相似度 最高的
// 候选。这样当多篇文档讲的是同一件事时，只有最相关的一篇会被保留，
// 剩余位置让给其它角度的内容。比较严格用 `>`（而非 >=）：同分时保持
// 靠前候选的优先权，与 Python 的 max/贪心行为一致，保证选择顺序稳定。
//
// 输入需已按相关性降序，函数内部取 score 作为相关性。
// 返回条目是输入的拷贝：MMR 常作为链路收口，调用方对结果做后处理
// （写诊断字段、缓存等）不应污染上游召回结果。
func SelectMMR(results []Item, topK int, lambda float64) []Item {
	if topK <= 0 || len(results) == 0 {
		return []Item{}
	}
	if len(results) <= topK {
		return copyItems(results)
	}

	// 词元集合只算一次：Jaccard 在贪心每轮都要对已选集合重算，
	// 预计算把整体复杂度从 O(n·k²·|text|) 降到 O(n·k²·|set|)
	tokens := make([]map[string]struct{}, len(results))
	for i, r := range results {
		tokens[i] = ItemTokenSet(r)
	}

	selected := make([]int, 0, topK)
	remaining := make([]int, len(results))
	for i := range remaining {
		remaining[i] = i
	}

	for len(remaining) > 0 && len(selected) < topK {
		bestPos, bestScore := 0, math.Inf(-1)
		for pos, idx := range remaining {
			relevance := toFloat(results[idx]["score"], 0)
			var mmr float64
			if len(selected) == 0 {
				// 首条没有冗余项，直接按相关性取
				mmr = relevance
			} else {
				redundancy := 0.0
				for _, s := range selected {
					if j := Jaccard(tokens[idx], tokens[s]); j > redundancy {
						redundancy = j
					}
				}
				mmr = lambda*relevance - (1-lambda)*redundancy
			}
			if mmr > bestScore {
				bestScore, bestPos = mmr, pos
			}
		}
		selected = append(selected, remaining[bestPos])
		remaining = append(remaining[:bestPos], remaining[bestPos+1:]...)
	}

	out := make([]Item, len(selected))
	for i, idx := range selected {
		out[i] = deepCopyItem(results[idx])
	}
	return out
}

func copyItems(src []Item) []Item {
	out := make([]Item, len(src))
	for i, r := range src {
		out[i] = deepCopyItem(r)
	}
	return out
}
