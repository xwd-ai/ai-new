package orders

import "strings"

// OrderStatusActive 是唯一视为有效的订单状态；退订、退款等一律不提供服务。
const OrderStatusActive = "active"

var scopeReasons = map[string]string{
	"no_order":             "未查询到订单记录，无法判断服务范围",
	"tier_unknown":         "无法识别所购版本，需要人工核实购买情况",
	"order_inactive":       "订单当前不是有效状态，服务已停止",
	"not_started":          "服务期尚未开始",
	"expired":              "服务期已结束",
	"period_unknown":       "订单起止时间不完整，无法判断服务期",
	"feature_unknown":      "无法识别要确认的功能",
	"feature_not_included": "当前版本不包含该功能",
	"in_service":           "在服务范围内",
}

// FeatureCheck 是功能判定的明细，解析失败时也保留用户原话以便回显。
type FeatureCheck struct {
	Query    string `json:"query"`
	Code     string `json:"code"`
	Name     string `json:"name"`
	Included bool   `json:"included"`
}

// Scope 是服务范围的综合判定结果，字段与 Python 实现保持一致。
type Scope struct {
	HasOrder        bool          `json:"has_order"`
	OrderID         *string       `json:"order_id"`
	OrderStatus     string        `json:"order_status"`
	Tier            *string       `json:"tier"`
	TierLabel       string        `json:"tier_label"`
	KnownTier       bool          `json:"known_tier"`
	Period          *Period       `json:"period"`
	InService       bool          `json:"in_service"`
	FeatureCodes    []string      `json:"feature_codes"`
	Features        []string      `json:"features"`
	Feature         *FeatureCheck `json:"feature"`
	FeatureIncluded bool          `json:"feature_included"`
	Allowed         bool          `json:"allowed"`
	ReasonCode      string        `json:"reason_code"`
	Reason          string        `json:"reason"`
}

// EvaluateServiceScope 综合版本、服务期与具体功能，判断能否按已购服务处理。
//
// 结论按业务优先级逐层收紧，命中即停：没有订单 → 版本不可识别 → 订单失效 →
// 服务期未开始 / 已到期 / 无法判定 → 功能不可识别 / 不在能力内。
// 先报最上游的问题，用户才知道该先解决哪一步；把「功能不在版本内」放在
// 「服务已到期」之前，会让人误以为升级版本就能用。
func EvaluateServiceScope(order *Order, feature string, now *float64) Scope {
	scope := Scope{
		HasOrder:        false,
		TierLabel:       "未知版本",
		FeatureCodes:    []string{},
		Features:        []string{},
		FeatureIncluded: false,
	}

	if order == nil {
		scope.ReasonCode = "no_order"
		scope.Reason = scopeReasons["no_order"]
		return scope
	}

	capabilities := TierCapabilities(order.ProductTier)
	period := EvaluateServicePeriod(&order.StartedAt, &order.ExpiresAt, now)
	orderStatus := strings.ToLower(compact(order.Status))

	// 只有明确的 active 才放行：状态为空或取值是历史脏数据时，
	// 订单究竟能不能用无法确认，按「不是有效状态」处理。
	statusBlocked := orderStatus != OrderStatusActive

	orderID := order.ID
	scope.HasOrder = true
	scope.OrderID = &orderID
	scope.OrderStatus = orderStatus
	scope.Tier = capabilities.Tier
	scope.TierLabel = capabilities.Label
	scope.KnownTier = capabilities.Known
	scope.Period = &period
	scope.FeatureCodes = capabilities.FeatureCodes
	scope.Features = capabilities.Features
	scope.InService = period.Active && !statusBlocked

	// 功能入参统一压空白后保存原文：解析失败时也要把用户问的内容回显出来，
	// 否则「无法识别」这条结论看不出是针对哪句话给的。
	query := compact(feature)
	var target *Feature
	if query != "" {
		target = ResolveFeature(query)
	}

	if query != "" {
		included := target != nil && capabilities.Known && containsCode(capabilities.FeatureCodes, target.Code)
		check := FeatureCheck{Query: query, Included: included}
		if target != nil {
			check.Code = target.Code
			check.Name = target.Name
		}
		scope.Feature = &check
		scope.FeatureIncluded = included
	} else {
		scope.FeatureIncluded = true
	}

	// 放行需要同时满足三件事：版本可识别、服务在期内、功能在版本内。
	// 版本不可识别时必须判为不放行 —— 判断不了版本还能不能用，等于判断不了权益。
	scope.Allowed = capabilities.Known && scope.InService && scope.FeatureIncluded

	checks := []struct {
		code string
		hit  bool
	}{
		{"tier_unknown", !capabilities.Known},
		{"order_inactive", statusBlocked},
		{"not_started", period.NotStarted},
		{"expired", period.Expired},
		{"period_unknown", period.Status == ServiceUnknown},
		{"feature_unknown", query != "" && target == nil},
		{"feature_not_included", target != nil && !scope.FeatureIncluded},
	}
	for _, c := range checks {
		if c.hit {
			scope.ReasonCode = c.code
			scope.Reason = scopeReasonText(c.code, &scope, target)
			return scope
		}
	}

	scope.ReasonCode = "in_service"
	scope.Reason = scopeReasons["in_service"]
	return scope
}

// scopeReasonText 把原因码补全成可展示的说明，带上版本名或功能名。
func scopeReasonText(code string, scope *Scope, target *Feature) string {
	text := scopeReasons[code]
	switch code {
	case "feature_unknown":
		if scope.Feature != nil {
			return text + "「" + scope.Feature.Query + "」"
		}
	case "feature_not_included":
		if target != nil {
			return scope.TierLabel + text + "「" + target.Name + "」"
		}
	case "not_started", "expired", "period_unknown":
		if scope.Period != nil {
			if code == "expired" && scope.Period.ExpiresText != "" {
				return text + "（" + scope.Period.ExpiresText + "）"
			}
			if code == "not_started" && scope.Period.StartedText != "" {
				return text + "（" + scope.Period.StartedText + " 起）"
			}
		}
	}
	return text
}

func containsCode(codes []string, want string) bool {
	for _, code := range codes {
		if code == want {
			return true
		}
	}
	return false
}
