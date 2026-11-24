// Package rank 实现与 Python backend/services/knowledge.py 精确对齐的
// 「检索后处理」确定性算法：加权 RRF 融合、重排复合分、阈值降级与 MMR 去冗余。
//
// 为什么单独成包且只用标准库：这段逻辑是 Python 侧的影子实现，供 HTTP /rank
// 端点调用；语义必须与 Python 逐字段一致（含 round(x, 4) 的四舍六入五成双），
// 任何偏差都会让影子比对失效。算法是纯函数——输入输出只依赖入参，不触碰
// 数据库、模型与配置文件，因此可以完整单测，也天然并发安全。
//
// Item 用 map 保真透传：Python 传来的结果条目可能带额外字段，结构体字段
// 会丢字段，因此一律用 map，算法只读写自己关心的键。
package rank

import (
	"fmt"
	"math"
	"sort"
	"strings"
)

// Item 是一条检索结果条目，字段与 Python 侧 dict 保持同名同义：
// id / title / kind / content / keywords / score / match_type 等，
// 额外字段原样透传。算法只读写自己关心的键。
type Item map[string]any

// Diagnostics 对齐 Python rerank_results(with_diagnostics=True) 返回的
// 诊断字典：threshold / effective_threshold / degraded / fallback /
// config / candidates / returned / skipped 等，键名与语义一一对应。
type Diagnostics map[string]any

// Options 是 Rank 的调用参数，对应 Python rerank_with_mmr 的关键字参数。
// Threshold / Lambda 为指针类型以区分「未指定」与「指定为零值」，
// 未指定时沿用 Config 中的值。
type Options struct {
	TopK      int
	Threshold *float64
	Lambda    *float64
	Config    Config
}

// Rank 是检索链路的收口函数，等价于 Python rerank_with_mmr：
// 召回（已完成）→ 复合分 + 阈值降级 → MMR 去冗余。
//
// rerankScores 的语义与 Python 侧 provider 返回的 (index, score) 一致：
// 下标指向「可打分候选」（build_passage 非空）在 scorable 中的位置，
// 长度必须与 results 等长（多余尾部位置视为填充，忽略不读）。
//   - rerankScores == nil：无重排模型 → 按 results[:candidate_count]
//     原样切片后交给 SelectMMR，不做阈值（对齐 rerank_results 的
//     no_provider 分支）。
//   - rerankScores != nil 且与 results 等长：走完整的「复合分 + 阈值降级
//   - MMR」路径（对齐 rerank_results 成功分支）。
//
// 返回的条目是输入的深拷贝：调用方改写返回值不会污染召回结果，
// 反之亦然——Python 侧虽是浅拷贝，但 HTTP 边界两侧互不可见，
// 深拷贝是更安全的等价实现。
func Rank(query string, results []Item, rerankScores []float64, opts Options) ([]Item, Diagnostics) {
	_ = query // 查询文本由上游喂给重排模型；本包只消费模型分，不再使用

	cfg := opts.Config
	// 调用方忘了给配置：零值配置会让 k/权重全部失效。RRFK 在合法配置中
	// 恒 >= 1（ParseConfig 的区间校验保证），为 0 即视为未初始化，回退默认。
	if cfg.RRFK == 0 {
		cfg = DefaultConfig()
	}
	if opts.Threshold != nil {
		cfg.Threshold = *opts.Threshold
	}
	if opts.Lambda != nil {
		cfg.MMRLambda = *opts.Lambda
	}
	topK := opts.TopK
	// 预取给 MMR 留候选空间：topK*3（对齐 Python candidate_count = max(top_k*3, top_k)）
	candidateCount := topK * 3
	if candidateCount < topK {
		candidateCount = topK
	}

	baseDiag := Diagnostics{
		"threshold":           cfg.Threshold,
		"effective_threshold": cfg.Threshold,
		"config":              cfg.toMap(),
	}
	if len(results) == 0 {
		// Python 在调用模型之前就短路：空候选不需要任何后处理
		return []Item{}, baseDiag
	}

	// degraded 降级路径：不重排、不过阈值，按原顺序切片后交给 MMR。
	// 对齐 Python 的 no_provider / no_passage / invalid_response 分支。
	degrade := func(reason string) ([]Item, Diagnostics) {
		diag := Diagnostics{
			"threshold":           cfg.Threshold,
			"effective_threshold": cfg.Threshold,
			"config":              cfg.toMap(),
			"skipped":             reason,
		}
		return SelectMMR(sliceByCount(results, candidateCount), topK, cfg.MMRLambda), diag
	}

	if rerankScores == nil {
		return degrade("no_provider")
	}
	if len(rerankScores) != len(results) {
		// 长度对不上说明上游映射出错：整批降级，避免错误位置的分数混入结果
		return degrade("invalid_response")
	}

	// 控制送审候选数量，避免无意义的打分成本（对齐 max_rerank_candidates）
	candidates := results
	if len(candidates) > cfg.MaxRerankCandidates {
		candidates = candidates[:cfg.MaxRerankCandidates]
	}
	// 无有效文本的候选无法打分：剔除后 scorableIdx[k] 记录第 k 个可打分
	// 候选在 candidates 中的原始下标，rerankScores[k] 与之一一对应
	scorableIdx := make([]int, 0, len(candidates))
	for i, c := range candidates {
		if strings.TrimSpace(BuildPassage(c)) != "" {
			scorableIdx = append(scorableIdx, i)
		}
	}
	if len(scorableIdx) == 0 {
		return degrade("no_passage")
	}
	for i := 0; i < len(scorableIdx); i++ {
		// Python 侧对非有限分整体抛错降级；Go 侧同样整批降级，
		// 防止 NaN 顺着复合分污染排序与阈值判断
		if math.IsNaN(rerankScores[i]) || math.IsInf(rerankScores[i], 0) {
			return degrade("invalid_response")
		}
	}

	ordered := make([]Item, 0, len(scorableIdx))
	for pos, idx := range scorableIdx {
		modelScore := clamp01(rerankScores[pos])
		item := deepCopyItem(candidates[idx])
		item["rerank_score"] = round4(modelScore)
		// 保留召回分便于对比重排前后变化；必须在新 score 写入**之前**
		// 取旧值（对齐 Python setdefault 的求值时机）
		if _, ok := item["recall_score"]; !ok {
			item["recall_score"] = toFloat(item["score"], 0)
		}
		item["score"] = round4(CompositeScore(item, modelScore, cfg))
		item["match_type"] = "rerank"
		ordered = append(ordered, item)
	}

	// 模型通常已按分数降序返回，这里再排一次以防上游未排序；
	// 稳定排序保持同分时的原顺序（对齐 Python Timsort 的稳定性）
	sort.SliceStable(ordered, func(i, j int) bool {
		return toFloat(ordered[i]["score"], 0) > toFloat(ordered[j]["score"], 0)
	})

	ordered, diag := ApplyThreshold(ordered, cfg.Threshold, cfg)
	diag["config"] = cfg.toMap()
	diag["candidates"] = len(scorableIdx)
	final := sliceByCount(ordered, candidateCount)
	diag["returned"] = len(final)

	return SelectMMR(final, topK, cfg.MMRLambda), diag
}

// sliceByCount 按 n 截断列表；n<=0 时返回原列表（对齐 Python
// `results[:top_k] if top_k else results` 中 0 为假不切片的行为）。
func sliceByCount(items []Item, n int) []Item {
	if n <= 0 || n >= len(items) {
		return items
	}
	return items[:n]
}

// CompositeScore 把重排分、召回分与来源权重混合成一个总分。
//
// 重排模型只判断「相关不相关」，召回分携带「好不好召回」的信息，
// 来源权重补偿不同类型资料的先验可信度；三者按配置系数线性混合后
// 压回 [0, 1]。返回值不取整，由调用处统一 round4（对齐 Python）。
func CompositeScore(item Item, rerankScore float64, cfg Config) float64 {
	// recall 优先取 recall_score，缺失时回退 score，再缺失按 0
	var recall float64
	if v, ok := item["recall_score"]; ok {
		recall = toFloat(v, 0)
	} else if v, ok := item["score"]; ok {
		recall = toFloat(v, 0)
	}
	kind, _ := item["kind"].(string)
	// kind 未命中来源权重表时取 0.5：不偏袒已知类型，也不一刀切清零
	sourceWeight := 0.5
	if w, ok := cfg.SourceWeights[kind]; ok {
		sourceWeight = w
	}
	blended := cfg.CompositeRerank*rerankScore +
		cfg.CompositeRecall*recall +
		cfg.CompositeSource*sourceWeight
	return clamp01(blended)
}

// ApplyThreshold 按阈值筛选候选，必要时放宽阈值或保底保留最优一条。
//
// 阈值比较的是**重排分**而非复合分：阈值回答的是「这段内容与问题是否
// 相关」，这是重排模型的判断；复合分还混入了召回分与来源权重，用它做
// 门槛会让召回容易的文档单靠召回分就通过。
//
// 三级降级：直接通过 → 放宽一次（不低于地板值）→ 保底保留最优一条
// （须达到保底分）→ 全空。诊断字段与 Python 逐键一致。
func ApplyThreshold(ordered []Item, threshold float64, cfg Config) ([]Item, Diagnostics) {
	diagnostics := Diagnostics{
		"threshold":           threshold,
		"effective_threshold": threshold,
		"degraded":            false,
		"fallback":            false,
	}
	if threshold <= 0 || len(ordered) == 0 {
		return ordered, diagnostics
	}

	relevance := func(item Item) float64 {
		if v, ok := item["rerank_score"]; ok {
			return toFloat(v, 0)
		}
		return toFloat(item["score"], 0)
	}

	if passing := filterByRelevance(ordered, relevance, threshold); len(passing) > 0 {
		return passing, diagnostics
	}

	// 第一次降级：放宽阈值后重试，但放宽后的阈值不能低于地板值
	if threshold > cfg.DegradeFloor {
		relaxed := math.Max(threshold*cfg.DegradeFactor, cfg.DegradeFloor)
		passing := filterByRelevance(ordered, relevance, relaxed)
		diagnostics["effective_threshold"] = relaxed
		diagnostics["degraded"] = true
		if len(passing) > 0 {
			return passing, diagnostics
		}
	}

	// 保底按模型相关性选优；相同相关性时保持第一出现的位置（对齐 Python max）
	best := 0
	for i := 1; i < len(ordered); i++ {
		if relevance(ordered[i]) > relevance(ordered[best]) {
			best = i
		}
	}
	diagnostics["fallback_candidate_id"] = ordered[best]["id"]
	diagnostics["fallback_candidate_score"] = relevance(ordered[best])
	diagnostics["fallback_min_score"] = cfg.FallbackMinScore
	if relevance(ordered[best]) >= cfg.FallbackMinScore {
		diagnostics["fallback"] = true
		return []Item{ordered[best]}, diagnostics
	}
	return []Item{}, diagnostics
}

func filterByRelevance(items []Item, relevance func(Item) float64, threshold float64) []Item {
	var passing []Item
	for _, r := range items {
		if relevance(r) >= threshold {
			passing = append(passing, r)
		}
	}
	return passing
}

// itemID 取条目的 id 字符串。Python 侧 id 是 dict 键参与排序与去重；
// Go 侧统一转成字符串比较。id 缺失时返回空串（Python 会 KeyError，
// 这里防御性兜底，语义上视所有缺失 id 为同一个条目）。
func itemID(item Item) string {
	switch v := item["id"].(type) {
	case string:
		return v
	case nil:
		return ""
	default:
		return fmt.Sprintf("%v", v)
	}
}

// toFloat 宽松取数：JSON 解出的数字、测试里手写的 int 都接受；
// 类型不符时返回 def（Python 侧 float() 会抛错，这里防御性降级）。
func toFloat(v any, def float64) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case float32:
		return float64(t)
	case int:
		return float64(t)
	case int64:
		return float64(t)
	case uint:
		return float64(t)
	case uint64:
		return float64(t)
	default:
		return def
	}
}

// clamp01 压回 [0, 1]。NaN 视作 0：Python 的 min(1.0, max(0.0, nan))
// 因 NaN 比较恒为假而落到 0，这里对齐该行为，避免 NaN 继续传播。
func clamp01(x float64) float64 {
	if math.IsNaN(x) {
		return 0
	}
	if x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}

// deepCopyItem 深拷贝一条结果：Rank 返回的条目允许调用方随意改写
// （含嵌套的 keywords 列表），不能因此污染召回阶段的输入。
func deepCopyItem(src Item) Item {
	dst := make(Item, len(src))
	for k, v := range src {
		dst[k] = deepCopyValue(v)
	}
	return dst
}

func deepCopyValue(v any) any {
	switch t := v.(type) {
	case Item:
		return deepCopyItem(t)
	case map[string]any:
		return deepCopyItem(t)
	case []any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = deepCopyValue(e)
		}
		return out
	case []string:
		out := make([]string, len(t))
		copy(out, t)
		return out
	case []Item:
		out := make([]Item, len(t))
		for i, e := range t {
			out[i] = deepCopyItem(e)
		}
		return out
	default:
		// string / float64 / bool / nil 等标量不可变，直接共享
		return v
	}
}
