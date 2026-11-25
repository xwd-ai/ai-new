package orders

import (
	"context"
	"database/sql"
	"fmt"
)

func ExpireOverdue(ctx context.Context, conn *sql.DB, limit int) (map[string]any, error) {
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	rows, err := tx.QueryContext(ctx, `
		SELECT id FROM orders WHERE status = 'active' AND expires_at <= ?
		ORDER BY expires_at, id LIMIT ?`, nowSeconds(), limit+1)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	remaining := max(0, len(ids)-limit)
	archived := ids[:len(ids)-remaining]
	for _, id := range archived {
		if _, err := tx.ExecContext(ctx, "UPDATE orders SET status = 'expired' WHERE id = ? AND status = 'active'", id); err != nil {
			return nil, err
		}
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	message := "没有需要归档的到期订单"
	if len(archived) > 0 {
		message = fmt.Sprintf("已归档 %d 个到期订单", len(archived))
	}
	return map[string]any{"limit": limit, "scanned": len(archived), "remaining": remaining,
		"has_more": remaining > 0, "archived": archived, "skipped": []string{}, "message": message}, nil
}
