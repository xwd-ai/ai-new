package rank

import (
	"math"
	"reflect"
	"testing"
)

// ---------------------------------------------------------------------------
// 测试辅助
// ---------------------------------------------------------------------------

func floatEq(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

func docItem(id, title, content string, keywords any, kind string, score float64) Item {
	return Item{
		"id":       id,
		"title":    title,
		"kind":     kind,
		"content":  content,
		"keywords": keywords,
		"score":    score,
	}
}

func idsOf(items []Item) []string {
	ids := make([]string, len(items))
	for i, it := range items {
		ids[i] = itemID(it)
	}
	return ids
}

func toSet(tokens []string) map[string]struct{} {
	set := make(map[string]struct{}, len(tokens))
	for _, t := range tokens {
		set[t] = struct{}{}
	}
	return set
}

func assertDiagFloat(t *testing.T, diag Diagnostics, key string, want float64) {
	t.Helper()
	got, ok := diag[key].(float64)
	if !ok || !floatEq(got, want) {
		t.Fatalf("diag[%q] = %v, want %v", key, diag[key], want)
	}
}

func assertDiagBool(t *testing.T, diag Diagnostics, key string, want bool) {
	t.Helper()
	if got, _ := diag[key].(bool); got != want {
		t.Fatalf("diag[%q] = %v, want %v", key, diag[key], want)
	}
}

// ---------------------------------------------------------------------------
// Tokenize
// ---------------------------------------------------------------------------

func TestTokenize(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want []string
	}{
		{"中英混排", "Hello, World! 你好世界 123", []string{"hello", "world", "你", "好", "世", "界", "123"}},
		{"大小写转小写", "ABC def GHiJ", []string{"abc", "def", "ghij"}},
		{"单字 CJK 逐字切分", "知识库", []string{"知", "识", "库"}},
		{"标点全部跳过", "!!!，。？！—", nil},
		{"空串", "", nil},
		{"字母数字连续段", "a1b2-c3d4", []string{"a1b2", "c3d4"}},
		{"中日韩扩展区外不切", "ぁあ", nil}, // 片假名不在 [U+4E00, U+9FFF]
		{"全角字母数字不匹配", "ＡＢＣ１２３", nil},
		{"中英紧邻切分", "退款refund政策", []string{"退", "款", "refund", "政", "策"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := Tokenize(tt.in)
			if len(got) != len(tt.want) {
				t.Fatalf("Tokenize(%q) = %v, want %v", tt.in, got, tt.want)
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Fatalf("Tokenize(%q)[%d] = %q, want %q", tt.in, i, got[i], tt.want[i])
				}
			}
		})
	}
}

// ---------------------------------------------------------------------------
// BuildPassage
// ---------------------------------------------------------------------------

func TestBuildPassage(t *testing.T) {
	tests := []struct {
		name string
		item Item
		want string
	}{
		{
			"完整字段",
			docItem("d1", "标题", "正文", []string{"kw1", "kw2"}, "faq", 0.5),
			"标题\nkw1、kw2\n正文",
		},
		{
			"关键词为 []any（JSON 形态）",
			docItem("d1", "标题", "正文", []any{"kw1", "kw2"}, "faq", 0.5),
			"标题\nkw1、kw2\n正文",
		},
		{"缺关键词", docItem("d1", "标题", "正文", nil, "faq", 0.5), "标题\n正文"},
		{"缺标题内容", docItem("d1", "", "", []string{"kw"}, "faq", 0.5), "kw"},
		{"全空", docItem("d1", "", "", []string{}, "faq", 0.5), ""},
		{"非 string 标题被忽略", Item{"title": 123, "content": "正文"}, "正文"},
		{"条目为空 map", Item{}, ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := BuildPassage(tt.item); got != tt.want {
				t.Fatalf("BuildPassage() = %q, want %q", got, tt.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Jaccard
// ---------------------------------------------------------------------------

func TestJaccard(t *testing.T) {
	tests := []struct {
		name string
		a, b map[string]struct{}
		want float64
	}{
		{"a 为空", nil, toSet([]string{"x"}), 0},
		{"b 为空", toSet([]string{"x"}), nil, 0},
		{"无交集", toSet([]string{"a", "b"}), toSet([]string{"c"}), 0},
		{"完全相同", toSet([]string{"a", "b"}), toSet([]string{"a", "b"}), 1},
		{"部分交集", toSet([]string{"a", "b"}), toSet([]string{"b", "c"}), 1.0 / 3.0},
		{"一个包含另一个", toSet([]string{"a", "b"}), toSet([]string{"a", "b", "c"}), 2.0 / 3.0},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := Jaccard(tt.a, tt.b); !floatEq(got, tt.want) {
				t.Fatalf("Jaccard() = %v, want %v", got, tt.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// round4（Python round(x, 4) 的四舍六入五成双）
// ---------------------------------------------------------------------------

func TestRound4(t *testing.T) {
	// 0.03125 = 2^-5 是二进制精确值，×10^4 恰为 312.5：
	// 半偶规则下 312 是偶数 → 舍到 0.0312（Python round(0.03125, 4) == 0.0312）
	// 0.09375 = 3×2^-5 → 937.5，937 是奇数 → 进位到 0.0938
	tests := []struct {
		in   float64
		want float64
	}{
		{0.03125, 0.0312},
		{0.09375, 0.0938},
		{-0.03125, -0.0312},
		{0.031250001, 0.0313},
		{0.031249999, 0.0312},
		{0.7, 0.7},
		{0, 0},
		{1.0, 1.0},
		{0.12344999, 0.1234},
		{0.12345001, 0.1235},
	}
	for _, tt := range tests {
		if got := round4(tt.in); !floatEq(got, tt.want) {
			t.Fatalf("round4(%v) = %v, want %v", tt.in, got, tt.want)
		}
	}
	// 负零保留符号位（对齐 Python round(-1e-5, 4) == -0.0 的输出形态）
	if got := round4(-1e-5); got != 0 || !math.Signbit(got) {
		t.Fatalf("round4(-1e-5) = %v (%v signbit=%v), want -0.0", got, math.Float64frombits(math.Float64bits(got)), math.Signbit(got))
	}
}

// ---------------------------------------------------------------------------
// FuseRRFWeighted
// ---------------------------------------------------------------------------

// 期望值按公式手算（k=60, vw=0.7, kw=0.3, maxRRF=1/61）：
//
//	A: 仅向量路第 1 名 → 0.7/61 ÷ (1/61) = 0.7
//	B: 向量第 2 + 关键词第 1 → 61*(0.7/62) + 0.3 = 0.988709... → round4 → 0.9887
//	C: 仅关键词路第 2 名 → 61*(0.3/62) = 0.295161... → round4 → 0.2952
func TestFuseRRFWeightedScoring(t *testing.T) {
	cfg := DefaultConfig()
	vector := []Item{
		docItem("a", "A 标题", "A 内容", []string{"ka"}, "faq", 0.9),
		docItem("b", "B 标题", "B 内容", []string{"kb"}, "faq", 0.8),
	}
	keyword := []Item{
		docItem("b", "", "", nil, "", 0), // 关键词路字段不全，验证向量路优先
		docItem("c", "C 标题", "C 内容", []string{"kc"}, "rule", 0.4),
	}

	fused := FuseRRFWeighted(vector, keyword, 3, cfg)

	if got := idsOf(fused); !reflect.DeepEqual(got, []string{"b", "a", "c"}) {
		t.Fatalf("ids = %v, want [b a c]", got)
	}
	wantScores := map[string]float64{"a": 0.7, "b": 0.9887, "c": 0.2952}
	wantTypes := map[string]string{"a": "vector", "b": "hybrid", "c": "keyword"}
	for _, it := range fused {
		id := itemID(it)
		if got, ok := it["score"].(float64); !ok || got != wantScores[id] {
			t.Fatalf("score[%s] = %v (%T), want %v", id, it["score"], it["score"], wantScores[id])
		}
		if got, _ := it["match_type"].(string); got != wantTypes[id] {
			t.Fatalf("match_type[%s] = %v, want %v", id, got, wantTypes[id])
		}
	}
	// b 的内容取自向量路（字段更完整）
	if title, _ := fused[0]["title"].(string); title != "B 标题" {
		t.Fatalf("b 的 title 应取向量路, got %q", title)
	}
	// 额外字段透传
	if kind, _ := fused[2]["kind"].(string); kind != "rule" {
		t.Fatalf("c 的 kind 透传失败, got %q", kind)
	}
}

func TestFuseRRFWeightedTieBreakByID(t *testing.T) {
	// 两路权重对称、两个不同 id 各列两路第 1 → 融合分相同（各 0.5），
	// 按分数降序、同分按 id 升序
	cfg := DefaultConfig()
	cfg.VectorWeight, cfg.KeywordWeight = 0.5, 0.5
	fused := FuseRRFWeighted(
		[]Item{docItem("zz", "t", "c", nil, "", 0)},
		[]Item{docItem("aa", "t", "c", nil, "", 0)},
		2, cfg,
	)
	if got := idsOf(fused); !reflect.DeepEqual(got, []string{"aa", "zz"}) {
		t.Fatalf("同分应按 id 升序, got %v", got)
	}
	for _, it := range fused {
		if s, _ := it["score"].(float64); s != 0.5 {
			t.Fatalf("score = %v, want 0.5", s)
		}
	}
}

func TestFuseRRFWeightedEdge(t *testing.T) {
	cfg := DefaultConfig()
	t.Run("两路都空", func(t *testing.T) {
		if got := FuseRRFWeighted(nil, nil, 3, cfg); len(got) != 0 {
			t.Fatalf("两路都空应返回空, got %v", got)
		}
	})
	t.Run("topK 截断", func(t *testing.T) {
		vector := []Item{docItem("a", "t", "c", nil, "", 0), docItem("b", "t", "c", nil, "", 0)}
		keyword := []Item{docItem("b", "t", "c", nil, "", 0), docItem("c", "t", "c", nil, "", 0)}
		fused := FuseRRFWeighted(vector, keyword, 1, cfg)
		if len(fused) != 1 || itemID(fused[0]) != "b" {
			t.Fatalf("topK=1 应只保留最高分 b, got %v", idsOf(fused))
		}
	})
	t.Run("topK<=0 返回空", func(t *testing.T) {
		vector := []Item{docItem("a", "t", "c", nil, "", 0)}
		if got := FuseRRFWeighted(vector, nil, 0, cfg); len(got) != 0 {
			t.Fatalf("topK=0 应返回空, got %v", idsOf(got))
		}
	})
	t.Run("同一路重复 id 保留最优名次", func(t *testing.T) {
		// a 在向量路出现两次（第 1、2 名），融合只用第 1 名；
		// a 同时在关键词路第 1 名 → hybrid，归一化分恰为 1.0
		vector := []Item{docItem("a", "t", "c", nil, "", 0), docItem("a", "t", "c", nil, "", 0)}
		keyword := []Item{docItem("a", "t", "c", nil, "", 0)}
		fused := FuseRRFWeighted(vector, keyword, 5, cfg)
		if len(fused) != 1 {
			t.Fatalf("重复 id 应合并为一条, got %v", idsOf(fused))
		}
		if s, _ := fused[0]["score"].(float64); s != 1.0 {
			t.Fatalf("双路第一名的归一化分应为 1.0, got %v", s)
		}
		if mt, _ := fused[0]["match_type"].(string); mt != "hybrid" {
			t.Fatalf("match_type 应为 hybrid, got %q", mt)
		}
	})
	t.Run("不污染输入", func(t *testing.T) {
		vector := []Item{docItem("a", "t", "c", nil, "", 0.3)}
		origScore := vector[0]["score"]
		_ = FuseRRFWeighted(vector, nil, 1, cfg)
		if vector[0]["score"] != origScore {
			t.Fatalf("输入条目的 score 被改写: %v -> %v", origScore, vector[0]["score"])
		}
	})
}

// ---------------------------------------------------------------------------
// CompositeScore
// ---------------------------------------------------------------------------

func TestCompositeScore(t *testing.T) {
	cfg := DefaultConfig()
	tests := []struct {
		name   string
		item   Item
		rerank float64
		want   float64
	}{
		{
			"faq 权重 0.8",
			Item{"recall_score": 0.5, "kind": "faq"},
			0.9,
			0.6*0.9 + 0.3*0.5 + 0.1*0.8, // 0.77
		},
		{
			"kind 未命中取 0.5",
			Item{"recall_score": 0.0, "kind": "unknown"},
			0.5,
			0.6*0.5 + 0.1*0.5, // 0.35
		},
		{
			"recall 缺失回退 score",
			Item{"score": 0.4, "kind": "rule"},
			0.5,
			0.6*0.5 + 0.3*0.4 + 0.1*1.0, // 0.52
		},
		{
			"recall 与 score 都缺失",
			Item{"kind": "rule"},
			0.5,
			0.6*0.5 + 0.1*1.0, // 0.4
		},
		{
			"上限 clamp 到 1",
			Item{"recall_score": 1.0, "kind": "rule"},
			1.0,
			1.0,
		},
		{
			"下限 clamp 到 0（负分）",
			Item{"recall_score": 0.0, "kind": "unknown"},
			0.0,
			0.05, // 0.1*0.5
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := CompositeScore(tt.item, tt.rerank, cfg); !floatEq(got, tt.want) {
				t.Fatalf("CompositeScore() = %v, want %v", got, tt.want)
			}
		})
	}
	// 负 rerank 分整体压到 0
	if got := CompositeScore(Item{"kind": "unknown"}, -1, cfg); got != 0 {
		t.Fatalf("负 rerank 分应 clamp 到 0, got %v", got)
	}
}

// ---------------------------------------------------------------------------
// ApplyThreshold
// ---------------------------------------------------------------------------

func TestApplyThreshold(t *testing.T) {
	cfg := DefaultConfig()
	mk := func(id string, rerank float64) Item {
		return Item{"id": id, "score": 0.1, "rerank_score": rerank}
	}

	t.Run("直接通过", func(t *testing.T) {
		ordered := []Item{mk("a", 0.9), mk("b", 0.45)}
		pass, diag := ApplyThreshold(ordered, 0.4, cfg)
		if len(pass) != 2 {
			t.Fatalf("应全部通过, got %d", len(pass))
		}
		assertDiagFloat(t, diag, "threshold", 0.4)
		assertDiagFloat(t, diag, "effective_threshold", 0.4)
		assertDiagBool(t, diag, "degraded", false)
		assertDiagBool(t, diag, "fallback", false)
	})

	t.Run("阈值非正直接返回", func(t *testing.T) {
		ordered := []Item{mk("a", 0.01)}
		pass, diag := ApplyThreshold(ordered, 0, cfg)
		if len(pass) != 1 || pass[0] == nil {
			t.Fatalf("threshold<=0 应原样返回")
		}
		assertDiagBool(t, diag, "degraded", false)
	})

	t.Run("放宽后通过", func(t *testing.T) {
		// threshold 0.8 → relaxed = max(0.8*0.7, 0.3) = 0.56
		ordered := []Item{mk("a", 0.6), mk("b", 0.2)}
		pass, diag := ApplyThreshold(ordered, 0.8, cfg)
		if len(pass) != 1 || itemID(pass[0]) != "a" {
			t.Fatalf("放宽后应保留 a, got %v", idsOf(pass))
		}
		assertDiagFloat(t, diag, "effective_threshold", 0.56)
		assertDiagBool(t, diag, "degraded", true)
		assertDiagBool(t, diag, "fallback", false)
	})

	t.Run("阈值不高于地板值时不放宽", func(t *testing.T) {
		// threshold 0.25 <= floor 0.3 → 跳过放宽直接保底
		ordered := []Item{mk("a", 0.2)}
		pass, diag := ApplyThreshold(ordered, 0.25, cfg)
		if len(pass) != 1 || itemID(pass[0]) != "a" {
			t.Fatalf("保底应保留 a, got %v", idsOf(pass))
		}
		assertDiagFloat(t, diag, "effective_threshold", 0.25)
		assertDiagBool(t, diag, "degraded", false)
		assertDiagBool(t, diag, "fallback", true)
	})

	t.Run("保底保留最优一条", func(t *testing.T) {
		ordered := []Item{mk("a", 0.5), mk("b", 0.5), mk("c", 0.2)}
		// 同为 0.5 时 Python max 取第一个 → a
		pass, diag := ApplyThreshold(ordered, 0.9, cfg)
		if len(pass) != 1 || itemID(pass[0]) != "a" {
			t.Fatalf("保底应取第一个最优 a, got %v", idsOf(pass))
		}
		assertDiagBool(t, diag, "fallback", true)
		assertDiagFloat(t, diag, "fallback_candidate_score", 0.5)
		assertDiagFloat(t, diag, "fallback_min_score", 0.15)
		if id, _ := diag["fallback_candidate_id"].(string); id != "a" {
			t.Fatalf("fallback_candidate_id = %v, want a", diag["fallback_candidate_id"])
		}
	})

	t.Run("连保底分都达不到返回空", func(t *testing.T) {
		ordered := []Item{mk("a", 0.05)}
		pass, diag := ApplyThreshold(ordered, 0.9, cfg)
		if len(pass) != 0 {
			t.Fatalf("低于保底分应返回空, got %v", idsOf(pass))
		}
		assertDiagBool(t, diag, "fallback", false)
		assertDiagFloat(t, diag, "fallback_candidate_score", 0.05)
		assertDiagFloat(t, diag, "effective_threshold", 0.63) // max(0.9*0.7, 0.3)
		assertDiagBool(t, diag, "degraded", true)
	})

	t.Run("空列表短路", func(t *testing.T) {
		pass, diag := ApplyThreshold(nil, 0.5, cfg)
		if len(pass) != 0 {
			t.Fatalf("空列表应返回空")
		}
		if _, ok := diag["fallback_candidate_id"]; ok {
			t.Fatalf("空列表不应产生保底诊断字段")
		}
	})
}

// ---------------------------------------------------------------------------
// SelectMMR
// ---------------------------------------------------------------------------

func TestSelectMMR(t *testing.T) {
	cfg := DefaultConfig()

	t.Run("去冗余：高度重合文档被挤掉", func(t *testing.T) {
		// A 与 B 内容几乎相同（token 全集相同），C 完全不同
		same := "退货政策 退货流程 退货说明"
		items := []Item{
			docItem("a", same, same, nil, "faq", 0.9),
			docItem("b", same, same, nil, "faq", 0.85),
			docItem("c", "会员权益", "会员积分规则", nil, "rule", 0.8),
		}
		got := SelectMMR(items, 2, cfg.MMRLambda)
		// 第 1 轮取 a；第 2 轮 b: 0.7*0.85-0.3*1 = 0.295，
		// c: 0.7*0.8-0 = 0.56 → 选 c
		if !reflect.DeepEqual(idsOf(got), []string{"a", "c"}) {
			t.Fatalf("MMR 应剔除冗余的 b, got %v", idsOf(got))
		}
	})

	t.Run("同分时保持靠前", func(t *testing.T) {
		items := []Item{
			docItem("a", "内容一", "甲", nil, "", 0.8),
			docItem("b", "内容一", "甲", nil, "", 0.8),
		}
		got := SelectMMR(items, 1, cfg.MMRLambda)
		if !reflect.DeepEqual(idsOf(got), []string{"a"}) {
			t.Fatalf("同分应选第一个, got %v", idsOf(got))
		}
	})

	t.Run("topK<=0 返回空", func(t *testing.T) {
		items := []Item{docItem("a", "t", "c", nil, "", 0.8)}
		if got := SelectMMR(items, 0, cfg.MMRLambda); len(got) != 0 {
			t.Fatalf("topK=0 应返回空, got %v", idsOf(got))
		}
	})

	t.Run("长度不足 topK 原样拷贝", func(t *testing.T) {
		items := []Item{
			docItem("a", "甲", "一", nil, "", 0.8),
			docItem("b", "乙", "二", nil, "", 0.5),
		}
		got := SelectMMR(items, 5, cfg.MMRLambda)
		if !reflect.DeepEqual(idsOf(got), []string{"a", "b"}) {
			t.Fatalf("不足 topK 应保持原顺序, got %v", idsOf(got))
		}
	})

	t.Run("返回拷贝不污染输入", func(t *testing.T) {
		items := []Item{docItem("a", "甲", "一", []string{"k"}, "", 0.8)}
		got := SelectMMR(items, 1, cfg.MMRLambda)
		got[0]["score"] = 99.0
		got[0]["keywords"].([]string)[0] = "污染"
		if items[0]["score"] != 0.8 {
			t.Fatalf("输入 score 被污染: %v", items[0]["score"])
		}
		if kw := items[0]["keywords"].([]string); kw[0] != "k" {
			t.Fatalf("输入 keywords 被污染: %v", kw)
		}
	})
}

// ---------------------------------------------------------------------------
// Rank
// ---------------------------------------------------------------------------

func TestRankNoRerankScores(t *testing.T) {
	cfg := DefaultConfig()
	results := []Item{
		docItem("d1", "退货", "退货需保留发票", []string{"退货"}, "manual", 0.9),
		docItem("d2", "发货", "48小时内发货", []string{"物流"}, "rule", 0.7),
		docItem("d3", "退款", "七天内可退款", []string{"退款"}, "faq", 0.5),
		docItem("d4", "会员", "会员享九折", nil, "faq", 0.3),
	}
	got, diag := Rank("退货怎么办理", results, nil, Options{TopK: 2, Config: cfg})

	if len(got) != 2 {
		t.Fatalf("最终应返回 2 条, got %d", len(got))
	}
	// 无模型：不重排、无复合分，仅 MMR 按原 score 去冗余
	if ids := idsOf(got); ids[0] != "d1" {
		t.Fatalf("第一条应为分数最高的 d1, got %v", ids)
	}
	if mt, ok := got[0]["match_type"].(string); ok && mt == "rerank" {
		// match_type 保留召回值，绝不应该是 "rerank"
		t.Fatalf("no_provider 分支不应打 rerank 标记, got %v", mt)
	}
	if _, ok := got[0]["rerank_score"]; ok {
		t.Fatalf("no_provider 分支不应写 rerank_score")
	}
	if reason, _ := diag["skipped"].(string); reason != "no_provider" {
		t.Fatalf("diag.skipped = %v, want no_provider", diag["skipped"])
	}
	assertDiagFloat(t, diag, "threshold", 0)
	if diag["config"] == nil {
		t.Fatalf("diag 应包含 config")
	}

	// 输入不被污染：返回条目是拷贝
	got[0]["score"] = -1.0
	if results[0]["score"] != 0.9 {
		t.Fatalf("输入条目被污染: %v", results[0]["score"])
	}
}

func TestRankWithRerankScores(t *testing.T) {
	cfg := DefaultConfig()
	results := []Item{
		docItem("d1", "退款政策", "七天内可退款", []string{"退款"}, "faq", 0.8),
		docItem("d2", "发货时间", "48小时内发货", []string{"物流"}, "rule", 0.6),
		docItem("d3", "退货说明", "退货需保留发票", []string{"退货"}, "manual", 0.4),
		docItem("d4", "会员权益", "会员享九折", nil, "unknown", 0.2),
	}
	// 全部 passage 非空 → rerankScores[i] 与第 i 个候选一一对应
	scores := []float64{0.9, 0.5, 0.7, 0.1}

	got, diag := Rank("怎么退款", results, scores, Options{TopK: 3, Config: cfg})

	// 复合分：d1=0.86, d3=0.6, d2=0.58, d4=0.17（降序）→
	// MMR 后（d1 与 d3 仅共享「退」一个 token）最终 [d1 d3 d2]
	if !reflect.DeepEqual(idsOf(got), []string{"d1", "d3", "d2"}) {
		t.Fatalf("ids = %v, want [d1 d3 d2]", idsOf(got))
	}
	want := []struct {
		id, mt        string
		rerank, score float64
		recall        float64
	}{
		{"d1", "rerank", 0.9, 0.86, 0.8},
		{"d3", "rerank", 0.7, 0.6, 0.4},
		{"d2", "rerank", 0.5, 0.58, 0.6},
	}
	for i, w := range want {
		it := got[i]
		if itemID(it) != w.id {
			t.Fatalf("got[%d].id = %s, want %s", i, itemID(it), w.id)
		}
		if mt, _ := it["match_type"].(string); mt != w.mt {
			t.Fatalf("got[%d].match_type = %v", i, it["match_type"])
		}
		if s, _ := it["rerank_score"].(float64); !floatEq(s, w.rerank) {
			t.Fatalf("got[%d].rerank_score = %v, want %v", i, s, w.rerank)
		}
		if s, _ := it["score"].(float64); !floatEq(s, w.score) {
			t.Fatalf("got[%d].score = %v, want %v", i, s, w.score)
		}
		// recall_score 取的是旧召回分（写入新 score 之前）
		if s, _ := it["recall_score"].(float64); !floatEq(s, w.recall) {
			t.Fatalf("got[%d].recall_score = %v, want %v", i, s, w.recall)
		}
	}

	// 诊断字段齐全（对齐 Python rerank_results 成功分支的 diag 键集）
	assertDiagFloat(t, diag, "threshold", 0)
	assertDiagFloat(t, diag, "effective_threshold", 0)
	assertDiagBool(t, diag, "degraded", false)
	assertDiagBool(t, diag, "fallback", false)
	if c, ok := diag["candidates"].(int); !ok || c != 4 {
		t.Fatalf("diag.candidates = %v, want 4", diag["candidates"])
	}
	if r, ok := diag["returned"].(int); !ok || r != 4 { // candidate_count = 9 >= 4
		t.Fatalf("diag.returned = %v, want 4", diag["returned"])
	}
	if diag["config"] == nil {
		t.Fatalf("diag 应包含 config")
	}

	// 深拷贝验证：改写返回值（含嵌套列表）不影响输入
	got[0]["score"] = -1.0
	got[0]["keywords"].([]string)[0] = "污染"
	if results[0]["score"] != 0.8 {
		t.Fatalf("输入 score 被污染: %v", results[0]["score"])
	}
	if kw := results[0]["keywords"].([]string); kw[0] != "退款" {
		t.Fatalf("输入 keywords 被污染: %v", kw)
	}
	if _, ok := results[0]["rerank_score"]; ok {
		t.Fatalf("输入不应被写入 rerank_score")
	}
}

func TestRankWithEmptyPassageAndPadding(t *testing.T) {
	cfg := DefaultConfig()
	results := []Item{
		docItem("empty", "", "", []string{}, "", 0.9), // 无可打分文本
		docItem("x", "标题X", "内容X", nil, "faq", 0.5),
		docItem("y", "标题Y", "内容Y", nil, "rule", 0.4),
	}
	// rerankScores 按 scorable 位置索引：[0] 对应 x、[1] 对应 y，
	// 尾部第 3 位是填充，被忽略
	scores := []float64{0.9, 0.8, 0.7}

	got, diag := Rank("q", results, scores, Options{TopK: 3, Config: cfg})

	if c, ok := diag["candidates"].(int); !ok || c != 2 {
		t.Fatalf("diag.candidates = %v, want 2", diag["candidates"])
	}
	byID := map[string]Item{}
	for _, it := range got {
		byID[itemID(it)] = it
	}
	if len(byID) != 2 {
		t.Fatalf("空 passage 条目不应出现, got %v", idsOf(got))
	}
	if s, _ := byID["x"]["rerank_score"].(float64); !floatEq(s, 0.9) {
		t.Fatalf("x 的 rerank_score = %v, want 0.9", byID["x"]["rerank_score"])
	}
	if s, _ := byID["y"]["rerank_score"].(float64); !floatEq(s, 0.8) {
		t.Fatalf("y 的 rerank_score = %v, want 0.8", byID["y"]["rerank_score"])
	}
}

func TestRankThresholdFallback(t *testing.T) {
	cfg := DefaultConfig()
	results := []Item{
		docItem("a", "标题甲", "内容甲", nil, "faq", 0.8),
		docItem("b", "标题乙", "内容乙", nil, "rule", 0.6),
	}
	scores := []float64{0.5, 0.3}
	threshold := 0.9

	got, diag := Rank("q", results, scores, Options{TopK: 2, Threshold: &threshold, Config: cfg})

	// 阈值 0.9 → 放宽到 max(0.63, 0.3)=0.63 仍不过 → 保底保留 rerank 最高的 a
	if len(got) != 1 || itemID(got[0]) != "a" {
		t.Fatalf("保底应只保留 a, got %v", idsOf(got))
	}
	assertDiagFloat(t, diag, "threshold", 0.9)
	assertDiagFloat(t, diag, "effective_threshold", 0.63)
	assertDiagBool(t, diag, "degraded", true)
	assertDiagBool(t, diag, "fallback", true)
	assertDiagFloat(t, diag, "fallback_candidate_score", 0.5)
	if id, _ := diag["fallback_candidate_id"].(string); id != "a" {
		t.Fatalf("fallback_candidate_id = %v, want a", diag["fallback_candidate_id"])
	}
}

func TestRankDegrades(t *testing.T) {
	cfg := DefaultConfig()
	results := []Item{
		docItem("a", "标题甲", "内容甲", nil, "faq", 0.9),
		docItem("b", "标题乙", "内容乙", nil, "rule", 0.7),
		docItem("c", "标题丙", "内容丙", nil, "rule", 0.5),
	}

	t.Run("长度不匹配整体降级", func(t *testing.T) {
		got, diag := Rank("q", results, []float64{0.9}, Options{TopK: 2, Config: cfg})
		if reason, _ := diag["skipped"].(string); reason != "invalid_response" {
			t.Fatalf("skipped = %v, want invalid_response", diag["skipped"])
		}
		// 降级路径不重排：保留原分数与 match_type，仅 MMR 去冗余
		if s, _ := got[0]["score"].(float64); !floatEq(s, 0.9) {
			t.Fatalf("降级应保留原 score, got %v", got[0]["score"])
		}
		if _, ok := got[0]["rerank_score"]; ok {
			t.Fatalf("降级不应写 rerank_score")
		}
		if len(got) != 2 {
			t.Fatalf("TopK=2 应返回 2 条, got %d", len(got))
		}
	})

	t.Run("非有限分数整体降级", func(t *testing.T) {
		got, diag := Rank("q", results, []float64{math.NaN(), 0.5, 0.5}, Options{TopK: 2, Config: cfg})
		if reason, _ := diag["skipped"].(string); reason != "invalid_response" {
			t.Fatalf("skipped = %v, want invalid_response", diag["skipped"])
		}
		if len(got) != 2 {
			t.Fatalf("got %d 条", len(got))
		}
	})

	t.Run("无可打分文本降级", func(t *testing.T) {
		blank := []Item{docItem("a", "", "", []string{}, "", 0.9), docItem("b", "", "", nil, "", 0.5)}
		got, diag := Rank("q", blank, []float64{0.9, 0.5}, Options{TopK: 2, Config: cfg})
		if reason, _ := diag["skipped"].(string); reason != "no_passage" {
			t.Fatalf("skipped = %v, want no_passage", diag["skipped"])
		}
		if len(got) != 2 {
			t.Fatalf("no_passage 降级应按原顺序返回, got %d", len(got))
		}
	})

	t.Run("空结果短路", func(t *testing.T) {
		got, diag := Rank("q", nil, nil, Options{TopK: 3, Config: cfg})
		if len(got) != 0 {
			t.Fatalf("空结果应返回空")
		}
		assertDiagFloat(t, diag, "threshold", 0)
		if _, ok := diag["skipped"]; ok {
			t.Fatalf("空结果短路不应有 skipped（对齐 Python 先于 no_provider 判断）")
		}
		if diag["config"] == nil {
			t.Fatalf("空结果也应带 config")
		}
	})
}

func TestRankCandidateCap(t *testing.T) {
	cfg := DefaultConfig() // MaxRerankCandidates = 200
	var results []Item
	var scores []float64
	for i := 0; i < 250; i++ {
		results = append(results, docItem("d", "标题", "内容文档", nil, "faq", 0.5))
		results[i]["id"] = string(rune('a'+i%26)) + string(rune('0'+i/26)) // 唯一 id
		scores = append(scores, 0.5)
	}
	got, diag := Rank("q", results, scores, Options{TopK: 10, Config: cfg})

	if c, ok := diag["candidates"].(int); !ok || c != 200 {
		t.Fatalf("candidates = %v, want 200", diag["candidates"])
	}
	if r, ok := diag["returned"].(int); !ok || r != 30 { // candidate_count = 30
		t.Fatalf("returned = %v, want 30", diag["returned"])
	}
	if len(got) != 10 {
		t.Fatalf("TopK=10 应返回 10 条, got %d", len(got))
	}
	// 各条 id 唯一（200 个之后的候选未参与重排，但也可能经 MMR 选中）
	seen := map[string]bool{}
	for _, it := range got {
		id := itemID(it)
		if seen[id] {
			t.Fatalf("出现重复 id %q", id)
		}
		seen[id] = true
	}
}

func TestRankConfigOverride(t *testing.T) {
	// Options.Threshold / Lambda 覆盖 Config
	results := []Item{docItem("a", "标题", "内容", nil, "rule", 0.9)}
	threshold, lambda := 0.5, 0.9
	_, diag := Rank("q", results, []float64{0.8}, Options{TopK: 1, Threshold: &threshold, Lambda: &lambda, Config: DefaultConfig()})
	assertDiagFloat(t, diag, "threshold", 0.5)
	conf, _ := diag["config"].(map[string]any)
	if conf == nil {
		t.Fatalf("diag.config 缺失")
	}
	if l, ok := conf["mmr_lambda"].(float64); !ok || !floatEq(l, 0.9) {
		t.Fatalf("config.mmr_lambda = %v, want 0.9", conf["mmr_lambda"])
	}
	if th, ok := conf["threshold"].(float64); !ok || !floatEq(th, 0.5) {
		t.Fatalf("config.threshold = %v, want 0.5", conf["threshold"])
	}
}

// ---------------------------------------------------------------------------
// ParseConfig
// ---------------------------------------------------------------------------

func assertConfigEq(t *testing.T, got, want Config) {
	t.Helper()
	if got.RRFK != want.RRFK || got.MaxRerankCandidates != want.MaxRerankCandidates ||
		!floatEq(got.VectorWeight, want.VectorWeight) || !floatEq(got.KeywordWeight, want.KeywordWeight) ||
		!floatEq(got.Threshold, want.Threshold) || !floatEq(got.DegradeFloor, want.DegradeFloor) ||
		!floatEq(got.DegradeFactor, want.DegradeFactor) || !floatEq(got.FallbackMinScore, want.FallbackMinScore) ||
		!floatEq(got.CompositeRerank, want.CompositeRerank) || !floatEq(got.CompositeRecall, want.CompositeRecall) ||
		!floatEq(got.CompositeSource, want.CompositeSource) || !floatEq(got.MMRLambda, want.MMRLambda) {
		t.Fatalf("config 不匹配:\n got  %+v\n want %+v", got, want)
	}
	if !reflect.DeepEqual(got.SourceWeights, want.SourceWeights) {
		t.Fatalf("source_weights 不匹配: got %v want %v", got.SourceWeights, want.SourceWeights)
	}
}

func TestParseConfig(t *testing.T) {
	t.Run("nil 与空 map 用默认", func(t *testing.T) {
		assertConfigEq(t, ParseConfig(nil), DefaultConfig())
		assertConfigEq(t, ParseConfig(map[string]any{}), DefaultConfig())
	})

	t.Run("部分覆盖其余默认", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"rrf_k":      100,
			"mmr_lambda": 0.5,
			"threshold":  0.25,
		})
		want := DefaultConfig()
		want.RRFK, want.MMRLambda, want.Threshold = 100, 0.5, 0.25
		assertConfigEq(t, got, want)
	})

	t.Run("JSON 数字（float64）形态", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"rrf_k":                 float64(80),
			"max_rerank_candidates": float64(100),
			"vector_weight":         0.6,
		})
		if got.RRFK != 80 || got.MaxRerankCandidates != 100 || !floatEq(got.VectorWeight, 0.6) {
			t.Fatalf("float64 形态解析失败: %+v", got)
		}
	})

	t.Run("类型错误回退默认", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"rrf_k":         "abc",
			"vector_weight": "high",
			"threshold":     true,
			"mmr_lambda":    []any{0.5},
		})
		assertConfigEq(t, got, DefaultConfig())
	})

	t.Run("越界回退默认", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"rrf_k":          0,
			"rrf_k2":         1001,
			"vector_weight":  1.5,
			"degrade_factor": 0.05, // 下界 0.1
			"threshold":      -0.1,
			"mmr_lambda":     2.0,
			"max_rerank":     0,
		})
		assertConfigEq(t, got, DefaultConfig())
		got = ParseConfig(map[string]any{"rrf_k": 1001, "max_rerank_candidates": 5000})
		if got.RRFK != 60 || got.MaxRerankCandidates != 200 {
			t.Fatalf("越界整数应回退: %+v", got)
		}
	})

	t.Run("两路权重同时归零回退默认配比", func(t *testing.T) {
		got := ParseConfig(map[string]any{"vector_weight": 0.0, "keyword_weight": 0.0})
		if !floatEq(got.VectorWeight, 0.7) || !floatEq(got.KeywordWeight, 0.3) {
			t.Fatalf("权重归零应回退 0.7/0.3: %+v", got)
		}
	})

	t.Run("source_weights 覆盖", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"source_weights": map[string]any{"rule": 0.9, "faq": "bad"},
		})
		wantWeights := map[string]float64{"rule": 0.9}
		if !reflect.DeepEqual(got.SourceWeights, wantWeights) {
			t.Fatalf("source_weights = %v, want %v", got.SourceWeights, wantWeights)
		}
		// faq 未命中 → 复合分里取 0.5
		if s := CompositeScore(Item{"kind": "faq", "recall_score": 0.2}, 0.2, got); !floatEq(s, 0.6*0.2+0.3*0.2+0.1*0.5) {
			t.Fatalf("未命中 kind 应取 0.5, got %v", s)
		}
	})

	t.Run("source_weights 全非法回退默认表", func(t *testing.T) {
		got := ParseConfig(map[string]any{"source_weights": map[string]any{"rule": "x"}})
		if !reflect.DeepEqual(got.SourceWeights, DefaultConfig().SourceWeights) {
			t.Fatalf("全非法应回退默认权重表, got %v", got.SourceWeights)
		}
	})

	t.Run("忽略不认识的键（含 prefetch_*）", func(t *testing.T) {
		got := ParseConfig(map[string]any{
			"prefetch_depth_floor": 99,
			"prefetch_multiplier":  9,
			"unknown_key":          "x",
		})
		assertConfigEq(t, got, DefaultConfig())
	})
}
