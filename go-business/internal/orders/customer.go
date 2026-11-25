package orders

import "fmt"

func EvaluateFeatureScope(rows []Order, feature string, now *float64) map[string]any {
	if now == nil {
		current := nowSeconds()
		now = &current
	}
	query := compact(feature)
	target := ResolveFeature(query)
	featureView := FeatureCheck{Query: query}
	if target != nil {
		featureView.Code, featureView.Name = target.Code, target.Name
	}

	items := make([]map[string]any, 0, len(rows))
	var primary, active, covered *Order
	activeCount := 0
	for i := range rows {
		order := &rows[i]
		scope := EvaluateServiceScope(order, query, now)
		items = append(items, map[string]any{
			"order_id": order.ID, "tier_label": scope.TierLabel, "order_status": scope.OrderStatus,
			"in_service": scope.InService, "feature_included": scope.FeatureIncluded,
			"reason_code": scope.ReasonCode, "reason": scope.Reason,
		})
		featureView.Included = featureView.Included || (query != "" && scope.FeatureIncluded)
		if primary == nil || rankLess(order, primary, now) {
			primary = order
		}
		if scope.InService {
			activeCount++
			if active == nil || rankLess(order, active, now) {
				active = order
			}
			if scope.FeatureIncluded && (covered == nil || rankLess(order, covered, now)) {
				covered = order
			}
		}
	}

	var coveredID any
	var code, reason string
	switch {
	case primary == nil:
		code, reason = "no_order", scopeReasons["no_order"]
		featureView.Code, featureView.Name = "", ""
	case query == "":
		code, reason = "feature_required", "请提供要确认的功能名称"
	case target == nil:
		code, reason = "feature_unknown", fmt.Sprintf("%s「%s」", scopeReasons["feature_unknown"], query)
	case covered != nil:
		coveredID, code = covered.ID, "in_scope"
		scope := EvaluateServiceScope(covered, query, now)
		reason = fmt.Sprintf("「%s」在服务范围内", target.Name)
		if scope.Period.ExpiresText != "" {
			reason += fmt.Sprintf("（%s，服务期至 %s，剩余 %d 天）",
				scope.TierLabel, scope.Period.ExpiresText, scope.Period.DaysRemaining)
		}
	default:
		if active != nil {
			primary = active
		}
		scope := EvaluateServiceScope(primary, query, now)
		code, reason = scope.ReasonCode, scope.Reason
	}
	return map[string]any{
		"has_order": len(rows) > 0, "feature": featureView, "in_scope": code == "in_scope",
		"covered_by": coveredID, "order_count": len(rows), "in_service_count": activeCount,
		"reason_code": code, "reason": reason, "message": reason, "orders": items,
	}
}

func RenewalHints(rows []Order, window int, now *float64) map[string]any {
	if now == nil {
		current := nowSeconds()
		now = &current
	}
	hints := make([]map[string]any, 0, len(rows))
	var due, upcoming int
	for i := range rows {
		hint := renewalHint(&rows[i], window, now)
		switch hint["state"] {
		case "due":
			due++
		case "upcoming":
			upcoming++
		}
		hints = append(hints, hint)
	}
	var message string
	switch {
	case len(rows) == 0:
		message = "未查询到订单记录，暂无续费提示"
	case due > 0 && upcoming > 0:
		message = fmt.Sprintf("%d 个订单已到期，另有 %d 个将在 %d 天内到期", due, upcoming, window)
	case due > 0:
		message = fmt.Sprintf("%d 个订单已到期，请及时续费", due)
	case upcoming > 0:
		message = fmt.Sprintf("%d 个订单将在 %d 天内到期，建议提前续费", upcoming, window)
	default:
		message = fmt.Sprintf("共 %d 个订单，暂无需续费", len(rows))
	}
	return map[string]any{
		"has_order": len(rows) > 0, "order_count": len(rows), "remind_count": due + upcoming,
		"due_count": due, "upcoming_count": upcoming, "window_days": window, "hints": hints, "message": message,
	}
}

func renewalHint(order *Order, window int, now *float64) map[string]any {
	scope := EvaluateServiceScope(order, "", now)
	period := scope.Period
	remaining := period.DaysRemaining
	overdue := 0
	var state, label, urgency, action, message string
	switch {
	case order.Status == "cancelled":
		state, label, urgency, action = "blocked", "已退订", "none", "buy_new"
		message = "订单已退订，不再提供服务，如需继续使用请重新购买"
	case order.Status == "expired":
		state, label, urgency, action = "due", "已到期", "critical", "renew"
		message = "订单已标记为过期，续费后恢复服务"
		if period.ExpiresText != "" {
			message += fmt.Sprintf("（原到期时间 %s）", period.ExpiresText)
		}
	case order.Status != "active":
		state, label, urgency, action = "unknown", "无法判定", "none", "verify"
		message = "订单状态无法识别，无法判断续费时间，需要人工核实"
	case period.Status == ServiceUnknown:
		state, label, urgency, action = "unknown", "无法判定", "none", "verify"
		message = "订单起止时间不完整，无法判断续费时间，需要人工核实"
	case period.NotStarted:
		state, label, urgency, action = "not_started", "未开始", "none", "wait"
		message = fmt.Sprintf("服务将于 %s 开始，暂无需续费", period.StartedText)
	case period.Expired:
		state, label, urgency, action = "due", "已到期", "critical", "renew"
		overdue = max(0, int((*now-*period.ExpiresAt)/86400))
		message = fmt.Sprintf("服务已于 %s 到期（已过期 %d 天），续费后可继续使用", period.ExpiresText, overdue)
	case remaining <= 7:
		state, label, urgency, action = "upcoming", "即将到期", "critical", "renew"
		message = fmt.Sprintf("服务将于 %s 到期，仅剩 %d 天，请尽快续费", period.ExpiresText, remaining)
	case remaining <= window:
		state, label, urgency, action = "upcoming", "即将到期", "warning", "renew"
		message = fmt.Sprintf("服务将于 %s 到期（剩余 %d 天），建议提前续费", period.ExpiresText, remaining)
	default:
		state, label, urgency, action = "active", "服务中", "none", "none"
		message = fmt.Sprintf("服务中，服务期至 %s（剩余 %d 天）", period.ExpiresText, remaining)
	}
	return map[string]any{
		"order_id": order.ID, "product_tier": order.ProductTier, "tier_label": scope.TierLabel,
		"order_status": scope.OrderStatus, "state": state, "state_label": label,
		"remind": state == "upcoming" || state == "due", "urgency": urgency, "suggested_action": action,
		"window_days": window, "days_remaining": remaining, "days_overdue": overdue,
		"expires_text": period.ExpiresText, "message": message,
	}
}
