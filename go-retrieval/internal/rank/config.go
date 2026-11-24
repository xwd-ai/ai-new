package rank

// Config 对齐 backend/services/retrieval_config.py 的 RetrievalConfig。
//
// 只保留会影响本包算法的参数；预取深度（prefetch_*）由上游召回阶段决定，
// 不属于后处理职责。零值 Config 不可用（RRFK 为 0），请用 DefaultConfig()
// 或 ParseConfig 构造。
type Config struct {
	RRFK, MaxRerankCandidates                                int
	VectorWeight, KeywordWeight                              float64
	Threshold, DegradeFloor, DegradeFactor, FallbackMinScore float64
	CompositeRerank, CompositeRecall, CompositeSource        float64
	MMRLambda                                                float64
	SourceWeights                                            map[string]float64
}

// DefaultConfig 的取值就是 Python 侧未配置任何覆盖时实际生效的数值。
// SourceWeights 每次返回新 map，避免多个调用方共享同一份可变底层数据。
func DefaultConfig() Config {
	return Config{
		RRFK:                60,
		MaxRerankCandidates: 200,
		VectorWeight:        0.7,
		KeywordWeight:       0.3,
		Threshold:           0.0,
		DegradeFloor:        0.3,
		DegradeFactor:       0.7,
		FallbackMinScore:    0.15,
		CompositeRerank:     0.6,
		CompositeRecall:     0.3,
		CompositeSource:     0.1,
		MMRLambda:           0.7,
		SourceWeights:       map[string]float64{"rule": 1.0, "faq": 0.8, "manual": 0.6},
	}
}

// 预取深度不属于本包职责，但 toMap 的诊断输出要求与 Python to_dict()
// 键集一致，这里以 Python 默认值占位，保证影子比对时字段齐全。
const (
	prefetchDepthFloor   = 50
	prefetchMultiplier   = 5
	defaultSourceWeight  = 0.5
	defaultVectorWeight  = 0.7
	defaultKeywordWeight = 0.3
)

// toMap 对齐 Python RetrievalConfig.to_dict() 的键名与取值，
// 用于诊断输出，便于确认本次实际用的是哪套参数。
func (c Config) toMap() map[string]any {
	weights := make(map[string]float64, len(c.SourceWeights))
	for k, v := range c.SourceWeights {
		weights[k] = v
	}
	return map[string]any{
		"prefetch_depth_floor":  prefetchDepthFloor,
		"prefetch_multiplier":   prefetchMultiplier,
		"rrf_k":                 c.RRFK,
		"vector_weight":         c.VectorWeight,
		"keyword_weight":        c.KeywordWeight,
		"max_rerank_candidates": c.MaxRerankCandidates,
		"threshold":             c.Threshold,
		"degrade_floor":         c.DegradeFloor,
		"degrade_factor":        c.DegradeFactor,
		"fallback_min_score":    c.FallbackMinScore,
		"mmr_lambda":            c.MMRLambda,
		"composite_rerank":      c.CompositeRerank,
		"composite_recall":      c.CompositeRecall,
		"composite_source":      c.CompositeSource,
		"source_weights":        weights,
	}
}

// ParseConfig 从 Python 传来的 RetrievalConfig.to_dict()（经 JSON 落成
// map[string]any）构造配置：缺省字段用默认值；类型错误或越界回退默认。
//
// 越界校验的区间与 Python load_retrieval_config 的环境变量校验一致——
// 配置错误的代价不该是「检索静默变差」，超出合理范围的取值一律忽略。
// 预取相关键（prefetch_*）不属于本包职责，直接忽略。
func ParseConfig(m map[string]any) Config {
	cfg := DefaultConfig()
	if m == nil {
		return cfg
	}
	if n, ok := getInt(m, "rrf_k"); ok && n >= 1 && n <= 1000 {
		cfg.RRFK = n
	}
	if n, ok := getInt(m, "max_rerank_candidates"); ok && n >= 1 && n <= 2000 {
		cfg.MaxRerankCandidates = n
	}
	if f, ok := getFloat(m, "vector_weight"); ok && f >= 0 && f <= 1 {
		cfg.VectorWeight = f
	}
	if f, ok := getFloat(m, "keyword_weight"); ok && f >= 0 && f <= 1 {
		cfg.KeywordWeight = f
	}
	if f, ok := getFloat(m, "threshold"); ok && f >= 0 && f <= 1 {
		cfg.Threshold = f
	}
	if f, ok := getFloat(m, "degrade_floor"); ok && f >= 0 && f <= 1 {
		cfg.DegradeFloor = f
	}
	if f, ok := getFloat(m, "degrade_factor"); ok && f >= 0.1 && f <= 1 {
		cfg.DegradeFactor = f
	}
	if f, ok := getFloat(m, "fallback_min_score"); ok && f >= 0 && f <= 1 {
		cfg.FallbackMinScore = f
	}
	if f, ok := getFloat(m, "composite_rerank"); ok && f >= 0 && f <= 1 {
		cfg.CompositeRerank = f
	}
	if f, ok := getFloat(m, "composite_recall"); ok && f >= 0 && f <= 1 {
		cfg.CompositeRecall = f
	}
	if f, ok := getFloat(m, "composite_source"); ok && f >= 0 && f <= 1 {
		cfg.CompositeSource = f
	}
	if f, ok := getFloat(m, "mmr_lambda"); ok && f >= 0 && f <= 1 {
		cfg.MMRLambda = f
	}
	if raw, ok := m["source_weights"].(map[string]any); ok {
		parsed := make(map[string]float64, len(raw))
		for k, v := range raw {
			if f, ok := asFloat(v); ok {
				parsed[k] = f
			}
		}
		// 至少有一个合法条目才整体替换：全非法说明结构本身不对，
		// 回退默认权重表而不是得到一张空表（空表会让所有 kind 落到 0.5）
		if len(parsed) > 0 {
			cfg.SourceWeights = parsed
		}
	}
	// 两路权重同时归零会让 RRF 归一化失效，回退默认配比（对齐 Python）
	if cfg.VectorWeight+cfg.KeywordWeight <= 0 {
		cfg.VectorWeight, cfg.KeywordWeight = defaultVectorWeight, defaultKeywordWeight
	}
	return cfg
}

func getFloat(m map[string]any, key string) (float64, bool) {
	v, ok := m[key]
	if !ok {
		return 0, false
	}
	return asFloat(v)
}

func asFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case float32:
		return float64(t), true
	case int:
		return float64(t), true
	case int64:
		return float64(t), true
	case uint:
		return float64(t), true
	case uint64:
		return float64(t), true
	default:
		// bool 是 JSON 数字之外的常见误型，Python 侧同样拒绝
		return 0, false
	}
}

func getInt(m map[string]any, key string) (int, bool) {
	v, ok := m[key]
	if !ok {
		return 0, false
	}
	switch t := v.(type) {
	case int:
		return t, true
	case int64:
		return int(t), true
	case float64:
		// JSON 数字统一落成 float64；整数值（60.0）视为 int，
		// 非整数（60.5）与 Python int() 语义不符，拒绝
		if t == float64(int(t)) {
			return int(t), true
		}
		return 0, false
	default:
		return 0, false
	}
}
