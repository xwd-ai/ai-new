package tickets

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

type CreateInput struct {
	UserID      string
	UserName    string
	Description string
	TriedSteps  string
	Evidence    string
}

var ErrEmptyDescription = errors.New("问题描述不能为空")

// Create creates a draft and its initial timeline entry in one SQLite write transaction.
func (w *Writer) Create(ctx context.Context, input CreateInput) (map[string]any, bool, error) {
	description := strings.TrimSpace(input.Description)
	if description == "" {
		return nil, false, ErrEmptyDescription
	}
	key := fmt.Sprintf("%x", sha256.Sum256([]byte(input.UserID+"::"+description)))
	tx, err := w.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, false, fmt.Errorf("开启建单事务: %w", err)
	}
	defer tx.Rollback()

	existing, err := scanTicket(tx.QueryRowContext(ctx, ticketSelect+" WHERE dedup_key = ?", key))
	switch {
	case err == nil && existing.Status != string(StatusResolved):
		return existing.payload(), false, nil
	case err == nil:
		// 释放已解决记录的去重键，历史工单本身及其时间线保留。
		if _, err := tx.ExecContext(ctx, "UPDATE tickets SET dedup_key = ? WHERE id = ?",
			"resolved:"+existing.ID, existing.ID); err != nil {
			return nil, false, fmt.Errorf("释放已解决工单去重键: %w", err)
		}
	case !errors.Is(err, sql.ErrNoRows):
		return nil, false, err
	}

	triedSteps := input.TriedSteps
	if triedSteps == "" {
		triedSteps, err = recordedSteps(ctx, tx, input.UserID)
		if err != nil {
			return nil, false, err
		}
	}
	id, err := allocateTicketID(ctx, tx)
	if err != nil {
		return nil, false, fmt.Errorf("分配工单号: %w", err)
	}

	ticket := Ticket{
		ID: id, DedupKey: key, UserID: input.UserID, UserName: input.UserName,
		Description: description, TriedSteps: triedSteps, Evidence: input.Evidence,
		Status: string(StatusPendingConfirm), CreatedAt: nowSeconds(), Replies: []any{},
	}
	_, err = tx.ExecContext(ctx, `
		INSERT INTO tickets (id, dedup_key, user_id, user_name, description, tried_steps, evidence, status, created_at, replies)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')`,
		ticket.ID, ticket.DedupKey, ticket.UserID, ticket.UserName, ticket.Description,
		ticket.TriedSteps, ticket.Evidence, ticket.Status, ticket.CreatedAt)
	if err != nil {
		return nil, false, fmt.Errorf("写入工单: %w", err)
	}
	if err := logTransition(ctx, tx, ticket.ID, "", ticket.Status, CleanOperator(input.UserName)); err != nil {
		return nil, false, err
	}
	if err := tx.Commit(); err != nil {
		return nil, false, fmt.Errorf("提交建单事务: %w", err)
	}
	return ticket.payload(), true, nil
}

func recordedSteps(ctx context.Context, tx *sql.Tx, userID string) (string, error) {
	rows, err := tx.QueryContext(ctx, "SELECT step FROM tried_steps WHERE user_id = ? ORDER BY at ASC", userID)
	if err != nil {
		return "", fmt.Errorf("读取排查步骤: %w", err)
	}
	defer rows.Close()

	var steps []string
	for rows.Next() {
		var step string
		if err := rows.Scan(&step); err != nil {
			return "", err
		}
		steps = append(steps, step)
	}
	return strings.Join(steps, " · "), rows.Err()
}

// Uses the same singleton counter and bootstrap rules as Python.
func allocateTicketID(ctx context.Context, tx *sql.Tx) (string, error) {
	var number int64
	err := tx.QueryRowContext(ctx, "UPDATE ticket_number_counter SET last_number = last_number + 1 WHERE id = 1 RETURNING last_number").Scan(&number)
	if errors.Is(err, sql.ErrNoRows) {
		err = tx.QueryRowContext(ctx, `
			INSERT INTO ticket_number_counter (id, last_number)
			SELECT 1, MAX(1000, COALESCE(MAX(CAST(substr(id, 3) AS INTEGER)), 0)) + 1
			FROM tickets
			WHERE substr(id, 1, 2) = 'T-' AND substr(id, 3) <> '' AND substr(id, 3) NOT GLOB '*[^0-9]*'
			RETURNING last_number`).Scan(&number)
	}
	if err != nil {
		return "", err
	}
	return "T-" + strconv.FormatInt(number, 10), nil
}
