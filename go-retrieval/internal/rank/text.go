package rank

import (
	"math"
	"math/big"
	"strings"
)

// Tokenize 切分为检索词元：英文/数字按连续词元、中文按单字，全部转小写。
//
// 与 Python 的正则 `[a-zA-Z0-9]+|[\u4e00-\u9fff]` 等价，但用 rune 扫描
// 手写——Go 的 regexp 不支持 \uXXXX 转义且引入逐字符回溯的开销。
// 注意小写化只作用于 ASCII 字母：正则本身只匹配 ASCII，CJK 单字的
// lower() 是恒等变换，逐 rune 处理即可完全对齐。
func Tokenize(text string) []string {
	tokens := make([]string, 0, 8)
	var ascii []byte
	flush := func() {
		if len(ascii) > 0 {
			tokens = append(tokens, string(ascii))
			ascii = ascii[:0]
		}
	}
	for _, r := range text {
		switch {
		case (r >= '0' && r <= '9') || (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z'):
			if r >= 'A' && r <= 'Z' {
				r += 'a' - 'A'
			}
			ascii = append(ascii, byte(r))
		case r >= 0x4e00 && r <= 0x9fff:
			// CJK 单字独立成词元，先结束进行中的 ASCII 词元
			flush()
			tokens = append(tokens, string(r))
		default:
			flush()
		}
	}
	flush()
	return tokens
}

// BuildPassage 把检索条目拼成送进重排模型的文本片段。
//
// 顺序为标题 → 关键词 → 正文：标题与关键词是判断主题的最强信号，
// 放在最前面可以让模型在长正文被截断时仍抓到重点。空片段跳过、
// 用换行连接，与 Python 侧逐字符一致（"、" 连接关键词）。
// 字段缺失或非 string 时安全降级为空串，不让脏数据中断整条链路。
func BuildPassage(item Item) string {
	parts := make([]string, 0, 3)
	if title, ok := item["title"].(string); ok && title != "" {
		parts = append(parts, title)
	}
	if keywords := joinKeywords(item["keywords"]); keywords != "" {
		parts = append(parts, keywords)
	}
	if content, ok := item["content"].(string); ok && content != "" {
		parts = append(parts, content)
	}
	return strings.Join(parts, "\n")
}

// joinKeywords 用中文顿号连接关键词列表，等价于 Python 的 "、".join(...)。
// 兼容 []string（Go 侧构造）与 []any（JSON 解码）；非字符串条目跳过
// （Python 会抛 TypeError，这里防御性忽略）。
func joinKeywords(v any) string {
	switch t := v.(type) {
	case []string:
		return strings.Join(t, "、")
	case []any:
		parts := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok {
				parts = append(parts, s)
			}
		}
		return strings.Join(parts, "、")
	default:
		return ""
	}
}

// Jaccard 计算两个词元集合的 Jaccard 相似度；任一为空或无交集时为 0。
// 交集/并集用 |a|+|b|-|交| 计算，避免再建一个并集 map。
func Jaccard(a, b map[string]struct{}) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	inter := 0
	for tok := range a {
		if _, ok := b[tok]; ok {
			inter++
		}
	}
	if inter == 0 {
		return 0
	}
	return float64(inter) / float64(len(a)+len(b)-inter)
}

// ItemTokenSet 取条目用于去重比对的词元集合。
//
// 这里保留单字：去重看的是「两段文字是否在讲同一件事」，单字的重叠
// 同样是有效信号。检索打分那侧过滤单字是为了压噪声，两个用途目标
// 不同，不能共用同一套筛子。
func ItemTokenSet(item Item) map[string]struct{} {
	set := make(map[string]struct{})
	for _, t := range Tokenize(BuildPassage(item)) {
		set[t] = struct{}{}
	}
	return set
}

var (
	roundScale = big.NewRat(10000, 1) // 10^4
	bigOne     = big.NewInt(1)
)

// round4 复现 Python round(x, 4)：四舍六入五成双（banker's rounding）。
//
// Go 的 math.Round 是「五远离零」，与 Python 在 .5 边界上结果不同；
// 影子比对要求逐字段一致，必须对齐舍入规则。同时不能简单用
// x*10000 再取整——浮点乘法本身会引入误差（如 0.2951*10^4 可能
// 落在 .5 的错误一侧），Python 的 round 作用于 double 的**精确**
// 二进制值。因此这里用 big.Rat 精确表示该值，放大后对精确的分数
// 部分做半偶判定，从根上消除浮点边界误差。
func round4(x float64) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) {
		return x
	}
	// SetFloat64 会精确表示该 double（非有限值已在上面拦截，不会 panic）
	r := new(big.Rat).SetFloat64(x)
	// 按绝对值处理再回填符号：round 对正负是对称的
	r.Abs(r)
	r.Mul(r, roundScale)
	q, rem := new(big.Int).QuoRem(r.Num(), r.Denom(), new(big.Int))
	// rem/den 与 1/2 精确比较：2*rem 与 den 比大小
	switch new(big.Int).Lsh(rem, 1).Cmp(r.Denom()) {
	case 1: // 小数部分 > 1/2：进位
		q.Add(q, bigOne)
	case 0: // 恰为 1/2：取最近偶数
		if q.Bit(0) == 1 {
			q.Add(q, bigOne)
		}
	}
	out := new(big.Rat).SetFrac(q, roundScale.Num()) // q / 10000
	if x < 0 {
		out.Neg(out)
	}
	f, _ := out.Float64()
	if x < 0 && f == 0 {
		// Python 对极小负数返回 -0.0；-0.0 与 0.0 数值相等，
		// 保留符号位只为 JSON 输出与 Python 完全一致
		return math.Copysign(0, -1)
	}
	return f
}
