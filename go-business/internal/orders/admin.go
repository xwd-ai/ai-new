package orders

import (
	"context"
	"database/sql"
	"fmt"
	"maps"
	"slices"
	"strings"
)

var statusLabels = map[string]string{"active": "有效", "expired": "过期", "cancelled": "退订"}

type AdminFilter struct {
	Statuses, Tiers            []string
	UserID, Keyword, Direction string
	Page, Size                 int
}

type adminCount struct {
	Key   string `json:"key"`
	Label string `json:"label"`
	Count int    `json:"count"`
}

func (r *Repository) AdminList(ctx context.Context, filter AdminFilter) (map[string]any, error) {
	from := " FROM orders WHERE 1=1"
	var args []any
	for _, item := range []struct {
		column   string
		selected []string
	}{
		{"status", filter.Statuses}, {"product_tier", filter.Tiers},
	} {
		if len(item.selected) != 0 {
			from += " AND " + item.column + " IN (?" + strings.Repeat(", ?", len(item.selected)-1) + ")"
			for _, key := range item.selected {
				args = append(args, key)
			}
		}
	}
	if filter.UserID != "" {
		from += " AND user_id = ?"
		args = append(args, filter.UserID)
	}
	if filter.Keyword != "" {
		from += " AND (LOWER(id) LIKE LOWER(?) ESCAPE '/' OR LOWER(user_id) LIKE LOWER(?) ESCAPE '/')"
		keyword := "%" + strings.NewReplacer("/", "//", "%", "/%", "_", "/_").Replace(filter.Keyword) + "%"
		args = append(args, keyword, keyword)
	}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(ctx, "SELECT status, product_tier, COUNT(*)"+from+" GROUP BY status, product_tier", args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	statusCounts, tierCounts := map[string]int{}, map[string]int{}
	total := 0
	for rows.Next() {
		var status, tier string
		var count int
		if err := rows.Scan(&status, &tier, &count); err != nil {
			return nil, err
		}
		statusCounts[status] += count
		tierCounts[tier] += count
		total += count
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	pageCount := (total + filter.Size - 1) / filter.Size
	items := []map[string]any{}
	if filter.Page <= pageCount {
		direction := "DESC"
		if filter.Direction == "asc" {
			direction = "ASC"
		}
		rows, err := tx.QueryContext(ctx, orderSelect+from+" ORDER BY expires_at "+direction+", id LIMIT ? OFFSET ?",
			append(args, filter.Size, (filter.Page-1)*filter.Size)...)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		now := nowSeconds()
		for rows.Next() {
			order, err := scanOrder(rows)
			if err != nil {
				return nil, err
			}
			items = append(items, adminOrderItem(order, EvaluateServiceScope(&order, "", &now)))
		}
		if err := rows.Err(); err != nil {
			return nil, err
		}
	}
	byStatus := []adminCount{}
	for _, status := range []string{"active", "expired", "cancelled"} {
		byStatus = append(byStatus, adminCount{status, statusLabels[status], statusCounts[status]})
	}
	for _, status := range slices.Sorted(maps.Keys(statusCounts)) {
		if statusLabels[status] == "" {
			byStatus = append(byStatus, adminCount{status, "未知状态", statusCounts[status]})
		}
	}
	byTier := []adminCount{}
	for _, tier := range []string{"basic", "pro"} {
		byTier = append(byTier, adminCount{tier, TierLabel(tier), tierCounts[tier]})
	}
	for _, tier := range slices.Sorted(maps.Keys(tierCounts)) {
		if _, known := NormalizeTier(tier); !known {
			byTier = append(byTier, adminCount{tier, "未知版本", tierCounts[tier]})
		}
	}
	return map[string]any{"items": items, "total": total, "page": filter.Page, "page_size": filter.Size,
		"pages": pageCount, "has_more": filter.Page < pageCount, "order": filter.Direction,
		"statuses": filter.Statuses, "tiers": filter.Tiers, "user_id": filter.UserID, "keyword": filter.Keyword,
		"by_status": byStatus, "by_tier": byTier, "message": adminListMessage(filter, total, pageCount, byStatus)}, nil
}

func adminListMessage(filter AdminFilter, total, pages int, counts []adminCount) string {
	var conditions []string
	var labels []string
	for _, status := range filter.Statuses {
		labels = append(labels, statusLabels[status])
	}
	if len(labels) > 0 {
		conditions = append(conditions, "状态 "+strings.Join(labels, "/"))
	}
	labels = labels[:0]
	for _, tier := range filter.Tiers {
		labels = append(labels, TierLabel(tier))
	}
	if len(labels) > 0 {
		conditions = append(conditions, "版本 "+strings.Join(labels, "/"))
	}
	if filter.UserID != "" {
		conditions = append(conditions, "客户 "+filter.UserID)
	}
	if filter.Keyword != "" {
		conditions = append(conditions, "关键词「"+filter.Keyword+"」")
	}
	scope := "共"
	if len(conditions) > 0 {
		scope = "按 " + strings.Join(conditions, " + ") + " 筛选到"
	}
	message := fmt.Sprintf("%s %d 个订单", scope, total)
	labels = labels[:0]
	for _, count := range counts {
		if count.Count > 0 {
			labels = append(labels, fmt.Sprintf("%s %d 个", count.Label, count.Count))
		}
	}
	if len(labels) > 0 {
		message += "：" + strings.Join(labels, "、")
	}
	if pages > 1 {
		message += fmt.Sprintf("，当前第 %d/%d 页", filter.Page, pages)
	}
	return message
}

const orderSelect = "SELECT id, user_id, product_tier, status, started_at, expires_at"

func scanOrder(row interface{ Scan(...any) error }) (Order, error) {
	var order Order
	err := row.Scan(&order.ID, &order.UserID, &order.ProductTier, &order.Status, &order.StartedAt, &order.ExpiresAt)
	return order, err
}

func adminOrderItem(order Order, scope Scope) map[string]any {
	label := statusLabels[order.Status]
	if label == "" {
		label = "未知状态"
	}
	return map[string]any{"order_id": order.ID, "user_id": order.UserID, "product_tier": order.ProductTier,
		"tier": scope.Tier, "tier_label": scope.TierLabel, "status": scope.OrderStatus, "status_label": label,
		"period": scope.Period, "in_service": scope.InService, "expires_text": scope.Period.ExpiresText,
		"days_remaining": scope.Period.DaysRemaining}
}

func ScopeMessage(scope Scope) string {
	if scope.ReasonCode != "in_service" {
		return scope.Reason
	}
	head := scope.TierLabel + "服务中"
	if scope.Feature != nil && scope.Feature.Included {
		head = fmt.Sprintf("%s包含「%s」", scope.TierLabel, scope.Feature.Name)
	}
	return fmt.Sprintf("%s，服务期至 %s（剩余 %d 天）", head, scope.Period.ExpiresText, scope.Period.DaysRemaining)
}

func (r *Repository) AdminDetail(ctx context.Context, id string) (map[string]any, error) {
	order, err := scanOrder(r.db.QueryRowContext(ctx, orderSelect+" FROM orders WHERE id = ?", compact(id)))
	if err != nil {
		return nil, err
	}
	now := nowSeconds()
	scope := EvaluateServiceScope(&order, "", &now)
	item := adminOrderItem(order, scope)
	view, err := r.ProductView(order.ProductTier)
	if err != nil {
		return nil, err
	}
	var count int
	if err := r.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM orders WHERE user_id = ?", order.UserID).Scan(&count); err != nil {
		return nil, err
	}
	allowed := []string{}
	label, known := statusLabels[order.Status]
	status := order.Status
	if !known {
		label, status = "未知状态", ""
	}
	switch status {
	case "active":
		allowed = append(allowed, "expired", "cancelled")
	case "expired":
		allowed = append(allowed, "active", "cancelled")
	}
	labels := make([]string, 0, len(allowed))
	for _, next := range allowed {
		labels = append(labels, statusLabels[next])
	}
	owner := order.UserID
	if owner == "" {
		owner = "未知"
	}
	message := fmt.Sprintf("订单 %s（客户 %s）：%s", order.ID, owner, ScopeMessage(scope))
	if known {
		target := strings.Join(labels, "、")
		if target == "" {
			target = "无，已结束流转"
		}
		message += fmt.Sprintf("；当前状态%s，可流转到%s", label, target)
	}
	maps.Copy(item, map[string]any{"product_name": view.Name, "allowed": scope.Allowed, "reason_code": scope.ReasonCode,
		"reason": scope.Reason, "features": view.Features, "user_order_count": count, "renewal": renewalHint(&order, 30, &now),
		"message": message, "status_flow": map[string]any{"order_id": order.ID, "status": status, "status_label": label,
			"known": known, "terminal": status == "cancelled", "allowed_statuses": allowed, "allowed_labels": labels}})
	return item, nil
}
