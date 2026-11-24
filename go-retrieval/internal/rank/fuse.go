package rank

import "sort"

// FuseRRFWeighted 用加权倒数排名融合（RRF）合并向量与关键词两路召回，
// 对齐 Python knowledge.py 的 fuse_rrf。
//
// 融合只依赖各自列表中的**名次**，不直接相加原始分数——向量相似度与
// BM25 分数量级不同，直接相加会让其中一路压倒另一路。权重（0.7/0.3）
// 允许两路的重要性不对称。
//
// 融合分再按理论最大值 (vw+kw)/(k+1) 归一化到 [0, 1]，使混合结果与
// 单路结果的分数仍可比较。同一路内重复 id 保留最优（最小）名次；
// 条目内容优先用向量路（字段更完整），缺失时用关键词路。
//
// 排序为（分数降序, id 升序）。id 在融合表内天然唯一，因此该排序是
// 全序——Go map 迭代顺序的不确定性被彻底消除，结果与 Python 的
// dict 插入顺序 + 稳定排序完全一致。
func FuseRRFWeighted(vectorResults, keywordResults []Item, topK int, cfg Config) []Item {
	if len(vectorResults) == 0 && len(keywordResults) == 0 {
		return []Item{}
	}
	k := float64(cfg.RRFK)
	vw, kw := cfg.VectorWeight, cfg.KeywordWeight

	vectorRank := make(map[string]int, len(vectorResults))
	keywordRank := make(map[string]int, len(keywordResults))
	items := make(map[string]Item, len(vectorResults)+len(keywordResults))

	for rank, item := range vectorResults {
		id := itemID(item)
		if _, ok := vectorRank[id]; !ok { // setdefault：重复 id 保留最优名次
			vectorRank[id] = rank + 1
		}
		if _, ok := items[id]; !ok {
			items[id] = item
		}
	}
	for rank, item := range keywordResults {
		id := itemID(item)
		if _, ok := keywordRank[id]; !ok {
			keywordRank[id] = rank + 1
		}
		if _, ok := items[id]; !ok {
			items[id] = item
		}
	}

	// 两路都排第一时融合分最大：(vw+kw)/(k+1)；<= 0 时归一化无意义
	maxRRF := (vw + kw) / (k + 1)

	fused := make([]Item, 0, len(items))
	for id, item := range items {
		var score float64
		inVector, inKeyword := false, false
		if r, ok := vectorRank[id]; ok {
			score += vw / (k + float64(r))
			inVector = true
		}
		if r, ok := keywordRank[id]; ok {
			score += kw / (k + float64(r))
			inKeyword = true
		}
		normalized := 0.0
		if maxRRF > 0 {
			normalized = score / maxRRF
		}
		// sources 顺序固定 vector 先、keyword 后：两路都有 → hybrid，
		// 单路 → 该路名称；理论上 items 必出现在至少一路中
		matchType := "keyword"
		switch {
		case inVector && inKeyword:
			matchType = "hybrid"
		case inVector:
			matchType = "vector"
		}

		// 浅拷贝 + 覆写分数与命中来源：额外字段原样保留（对齐 {**item, ...}）
		out := make(Item, len(item)+2)
		for key, v := range item {
			out[key] = v
		}
		out["score"] = round4(clamp01(normalized))
		out["match_type"] = matchType
		fused = append(fused, out)
	}

	sort.Slice(fused, func(i, j int) bool {
		si, sj := toFloat(fused[i]["score"], 0), toFloat(fused[j]["score"], 0)
		if si != sj {
			return si > sj
		}
		return itemID(fused[i]) < itemID(fused[j])
	})

	// topK<=0 返回空。Python 的 fused[:top_k] 在负数下是「去掉尾部」的
	// 切片语义，属于依赖方从不会触发的边界；这里取更安全的空结果。
	if topK <= 0 {
		return []Item{}
	}
	if len(fused) > topK {
		fused = fused[:topK]
	}
	return fused
}
