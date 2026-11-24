package orders

import "strings"

// Feature 是一项可销售的能力。
//
// Code 是判定用的稳定标识，Name 给用户看，Aliases 收口语与历史叫法
// （「人工客服」「API」这类），让输入不必与文案逐字一致。
type Feature struct {
	Code    string
	Name    string
	Aliases []string
}

// 版本标识。只有这两个取值是合法的，其它一律判为未知。
const (
	TierBasic = "basic"
	TierPro   = "pro"
)

var (
	featBasicExport      = Feature{"export.basic", "基础导出", []string{"导出", "单文件导出"}}
	featSingleUpload     = Feature{"upload.single", "单文件上传", []string{"上传", "单文件上传"}}
	featCommunitySupport = Feature{"support.community", "社区支持", []string{"社区支持", "社区"}}
	featBatchExport      = Feature{"export.batch", "批量导出", nil}
	featAdvancedReport   = Feature{"report.advanced", "高级报表", []string{"报表"}}
	featPrioritySupport  = Feature{"support.priority", "优先客服支持", []string{"优先客服", "人工客服"}}
	featAPIAccess        = Feature{"api.access", "API 接口", []string{"api", "接口"}}
)

// 基础版：能用起来的最小集合
var basicCapabilities = []Feature{featBasicExport, featSingleUpload, featCommunitySupport}

// 专业版在基础版之上叠加，能力只增不减 —— 升级版本不该丢掉已有功能。
var proCapabilities = []Feature{
	featBasicExport, featSingleUpload, featCommunitySupport,
	featBatchExport, featAdvancedReport, featPrioritySupport, featAPIAccess,
}

var tierFeatures = map[string][]Feature{
	TierBasic: basicCapabilities,
	TierPro:   proCapabilities,
}

var tierLabels = map[string]string{
	TierBasic: "基础版",
	TierPro:   "专业版",
}

// 版本别名：接口、订单、用户话术里出现过的写法都收敛到两个标准取值
var tierAliases = map[string]string{
	"basic": TierBasic, "standard": TierBasic, "基础": TierBasic, "基础版": TierBasic,
	"pro": TierPro, "professional": TierPro, "premium": TierPro, "专业": TierPro, "专业版": TierPro,
}

// allFeatures 是去重后的全部能力，保持声明顺序 —— 顺序稳定命中结果才可复现。
var allFeatures = func() []Feature {
	seen := make(map[string]bool, 8)
	out := make([]Feature, 0, 8)
	for _, tier := range []string{TierBasic, TierPro} {
		for _, f := range tierFeatures[tier] {
			if !seen[f.Code] {
				seen[f.Code] = true
				out = append(out, f)
			}
		}
	}
	return out
}()

// NormalizeTier 把版本入参收敛为 basic / pro，无法识别返回 ("", false)。
//
// 大小写与首尾空白先归一；拼错的、留空的、历史脏数据一律判为未知，
// **不猜也不落默认版本** —— 猜错版本会让服务范围判定直接给出错误结论。
func NormalizeTier(tier string) (string, bool) {
	key, ok := tierAliases[strings.ToLower(strings.TrimSpace(tier))]
	return key, ok
}

// TierLabel 返回版本显示名，未知版本返回「未知版本」。
func TierLabel(tier string) string {
	if key, ok := NormalizeTier(tier); ok {
		return tierLabels[key]
	}
	return "未知版本"
}

// ResolveFeature 把一段描述解析成能力项，解析不出返回 nil。
//
// 先按编码与全名精确匹配，避免标准名称被模糊规则带偏；
// 再做单向包含匹配并取**命中最长**的一条 —— 「批量导出失败」应落在批量导出，
// 而不是退化成基础导出。只做「能力名出现在描述里」这一向，
// 否则一句「导出」会被更长的「批量导出」抢走，基础版用户反而被判超范围。
func ResolveFeature(text string) *Feature {
	value := strings.ToLower(compact(text))
	if value == "" {
		return nil
	}

	for i := range allFeatures {
		f := &allFeatures[i]
		if value == strings.ToLower(f.Code) || value == strings.ToLower(f.Name) {
			return f
		}
	}

	var best *Feature
	bestLen := 0
	for i := range allFeatures {
		f := &allFeatures[i]
		tokens := append([]string{strings.ToLower(f.Name)}, lowerAll(f.Aliases)...)
		for _, token := range tokens {
			if token != "" && strings.Contains(value, token) && len(token) > bestLen {
				best, bestLen = f, len(token)
			}
		}
	}
	return best
}

// Capabilities 是版本能力的展示形态。
type Capabilities struct {
	Tier         *string  `json:"tier"`
	Label        string   `json:"label"`
	Known        bool     `json:"known"`
	FeatureCodes []string `json:"feature_codes"`
	Features     []string `json:"features"`
}

// TierCapabilities 列出版本包含的能力，未知版本返回 Known=false 与空列表。
func TierCapabilities(tier string) Capabilities {
	key, ok := NormalizeTier(tier)
	if !ok {
		return Capabilities{
			Tier: nil, Label: TierLabel(tier), Known: false,
			FeatureCodes: []string{}, Features: []string{},
		}
	}

	features := tierFeatures[key]
	codes := make([]string, 0, len(features))
	names := make([]string, 0, len(features))
	for _, f := range features {
		codes = append(codes, f.Code)
		names = append(names, f.Name)
	}
	tierKey := key
	return Capabilities{
		Tier: &tierKey, Label: tierLabels[key], Known: true,
		FeatureCodes: codes, Features: names,
	}
}

// HasCapability 判断版本是否包含某项能力；功能名无法识别时按不包含处理。
//
// 无法识别不等于包含：宁可让调用方去核实，也不放行一个判断不了的功能。
func HasCapability(tier, feature string) bool {
	target := ResolveFeature(feature)
	key, ok := NormalizeTier(tier)
	if target == nil || !ok {
		return false
	}
	for _, f := range tierFeatures[key] {
		if f.Code == target.Code {
			return true
		}
	}
	return false
}

func lowerAll(values []string) []string {
	out := make([]string, len(values))
	for i, v := range values {
		out[i] = strings.ToLower(v)
	}
	return out
}
