package tickets

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"slices"
	"strings"
)

func (r *Repository) Statistics(ctx context.Context, userID string, days *int) (map[string]any, string, error) {
	now := nowSeconds()
	var since any
	var scopeUser any
	filter := " WHERE 1=1"
	var args []any
	if userID != "" {
		scopeUser = userID
		filter += " AND t.user_id = ?"
		args = append(args, userID)
	}
	if days != nil {
		since = now - float64(*days)*86400
		filter += " AND t.created_at >= ?"
		args = append(args, since)
	}

	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()

	counts := map[Status]int{}
	rows, err := tx.QueryContext(ctx, "SELECT t.status, COUNT(*) FROM tickets t"+filter+" GROUP BY t.status", args...)
	if err != nil {
		return nil, "", err
	}
	defer rows.Close()
	total := 0
	for rows.Next() {
		var status Status
		var count int
		if err := rows.Scan(&status, &count); err != nil {
			return nil, "", err
		}
		counts[status] = count
		total += count
	}
	if err := rows.Err(); err != nil {
		return nil, "", err
	}

	durations := map[Status][]float64{}
	rows, err = tx.QueryContext(ctx, `
		SELECT t.status, MAX(l.changed_at) - t.created_at FROM tickets t
		JOIN ticket_status_logs l ON t.id = l.ticket_id AND l.to_status IN ('resolved', 'closed')`+
		filter+" AND t.status IN ('resolved', 'closed') GROUP BY t.id, t.status, t.created_at", args...)
	if err != nil {
		return nil, "", err
	}
	defer rows.Close()
	for rows.Next() {
		var status Status
		var seconds float64
		if err := rows.Scan(&status, &seconds); err != nil {
			return nil, "", err
		}
		if seconds >= 0 {
			durations[status] = append(durations[status], seconds)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, "", err
	}

	percent := func(count int) float64 {
		if total == 0 {
			return 0
		}
		return math.RoundToEven(float64(count)*1000/float64(total)) / 10
	}
	states := []Status{StatusOpen, StatusInProgress, StatusResolved, StatusPendingConfirm, StatusTransferredHuman, StatusClosed}
	labels := []string{"待处理", "处理中", "已解决", "待确认", "已转人工", "已关闭"}
	publicCounts := map[Status]int{}
	breakdown := make([]map[string]any, 0, len(states))
	var parts []string
	for i, status := range states {
		count := counts[status]
		publicCounts[status] = count
		breakdown = append(breakdown, map[string]any{"status": status, "label": labels[i], "count": count, "percent": percent(count)})
		if count > 0 {
			parts = append(parts, fmt.Sprintf("%s %d 个", labels[i], count))
		}
	}
	active := counts[StatusOpen] + counts[StatusInProgress] + counts[StatusPendingConfirm] + counts[StatusTransferredHuman]
	settled := counts[StatusResolved] + counts[StatusClosed]
	all := slices.Concat(durations[StatusResolved], durations[StatusClosed])
	summary := durationSummary(all)
	summary["by_status"] = map[Status]any{
		StatusResolved: durationSummary(durations[StatusResolved]),
		StatusClosed:   durationSummary(durations[StatusClosed]),
	}
	message := fmt.Sprintf("共 %d 个工单", total)
	if len(parts) > 0 {
		message += "：" + strings.Join(parts, "、")
	}
	if len(all) > 0 {
		message += fmt.Sprintf("；平均处理时长 %s（样本 %d 个，闭环率 %.1f%%）", summary["avg_text"], len(all), percent(settled))
	} else {
		message += "；暂无已结案工单可统计处理时长"
	}
	if days != nil {
		message += fmt.Sprintf("（统计最近 %d 天创建）", *days)
	}
	return map[string]any{
		"total": total, "status_counts": publicCounts, "status_breakdown": breakdown,
		"active":            map[string]any{"count": active, "statuses": []Status{StatusOpen, StatusInProgress, StatusPendingConfirm, StatusTransferredHuman}, "percent": percent(active)},
		"settled":           map[string]any{"count": settled, "statuses": []Status{StatusResolved, StatusClosed}, "percent": percent(settled), "completion_rate": percent(settled)},
		"handling_duration": summary, "range": map[string]any{"days": days, "since": since, "until": now},
		"user_id": scopeUser, "generated_at": now,
	}, message, nil
}

func durationSummary(values []float64) map[string]any {
	if len(values) == 0 {
		return map[string]any{"sample_size": 0, "total_seconds": nil, "avg_seconds": nil,
			"min_seconds": nil, "max_seconds": nil, "median_seconds": nil, "avg_text": "—"}
	}
	slices.Sort(values)
	round := func(value float64) float64 { return math.RoundToEven(value*1000) / 1000 }
	var total float64
	for _, value := range values {
		total += value
	}
	avg := total / float64(len(values))
	median := (values[(len(values)-1)/2] + values[len(values)/2]) / 2
	return map[string]any{
		"sample_size": len(values), "total_seconds": round(total), "avg_seconds": round(avg),
		"min_seconds": round(values[0]), "max_seconds": round(values[len(values)-1]),
		"median_seconds": round(median), "avg_text": formatDuration(avg),
	}
}

func formatDuration(seconds float64) string {
	total := int(math.RoundToEven(max(0, seconds)))
	switch {
	case total < 60:
		return fmt.Sprintf("%d 秒", total)
	case total < 3600:
		return fmt.Sprintf("%d 分钟", total/60)
	case total < 86400:
		if minutes := total / 60 % 60; minutes != 0 {
			return fmt.Sprintf("%d 小时 %d 分", total/3600, minutes)
		}
		return fmt.Sprintf("%d 小时", total/3600)
	default:
		if hours := total / 3600 % 24; hours != 0 {
			return fmt.Sprintf("%d 天 %d 小时", total/86400, hours)
		}
		return fmt.Sprintf("%d 天", total/86400)
	}
}
