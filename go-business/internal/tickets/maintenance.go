package tickets

import (
	"aibusiness/internal/db"
	"context"
	"database/sql"
	"errors"
	"fmt"
)

func (w *Writer) Reply(ctx context.Context, id, replier, content string) (map[string]any, error) {
	tx, err := db.BeginWrite(ctx, w.db)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	ticket, err := w.loadForWrite(ctx, tx, id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	reply := map[string]any{"replier": replier, "content": content, "at": nowSeconds()}
	if ticket.Status == string(StatusInProgress) {
		ticket.Replies = append(ticket.Replies, reply)
		encoded, err := encodeReplies(ticket.Replies)
		if err != nil {
			return nil, err
		}
		if _, err := tx.ExecContext(ctx, "UPDATE tickets SET replies = ? WHERE id = ?", encoded, id); err != nil {
			return nil, err
		}
	} else if err := applyTransition(ctx, tx, ticket, StatusInProgress, replier, reply); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return ticket.payload(), nil
}

type ExpirationResult struct {
	TimeoutSeconds int      `json:"timeout_seconds"`
	Deadline       float64  `json:"deadline"`
	Scanned        int      `json:"scanned"`
	Closed         int      `json:"closed"`
	ClosedIDs      []string `json:"closed_ids"`
	Remaining      int      `json:"remaining"`
}

func (w *Writer) ExpirePending(ctx context.Context, timeout, limit int) (ExpirationResult, error) {
	now := nowSeconds()
	result := ExpirationResult{TimeoutSeconds: timeout, Deadline: now - float64(timeout), ClosedIDs: []string{}}
	tx, err := db.BeginWrite(ctx, w.db)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()

	const filter = " FROM tickets WHERE status = ? AND created_at <= ?"
	rows, err := tx.QueryContext(ctx, db.LockRows(w.db, "SELECT id"+filter+" ORDER BY created_at, id LIMIT ?"),
		StatusPendingConfirm, result.Deadline, limit)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return result, err
		}
		result.ClosedIDs = append(result.ClosedIDs, id)
	}
	if err := rows.Err(); err != nil {
		return result, err
	}
	if err := rows.Close(); err != nil {
		return result, err
	}
	for _, id := range result.ClosedIDs {
		ticket, err := w.loadForWrite(ctx, tx, id)
		if err != nil {
			return result, err
		}
		reply := map[string]any{
			"replier": systemReplier,
			"content": fmt.Sprintf("待确认超时（阈值 %d 秒）自动关闭", timeout),
			"at":      now,
		}
		if err := applyTransition(ctx, tx, ticket, StatusClosed, DefaultOperator, reply); err != nil {
			return result, err
		}
	}
	result.Scanned = len(result.ClosedIDs)
	result.Closed = result.Scanned
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*)"+filter, StatusPendingConfirm, result.Deadline).Scan(&result.Remaining); err != nil {
		return result, err
	}
	return result, tx.Commit()
}
