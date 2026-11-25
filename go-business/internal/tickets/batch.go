package tickets

import (
	"context"
	"database/sql"
	"errors"
)

type BatchFailure struct {
	TicketID   string    `json:"ticket_id"`
	Code       ErrorCode `json:"error_code"`
	Reason     string    `json:"reason"`
	FromStatus string    `json:"from_status"`
}

type BatchResult struct {
	Action       string         `json:"action"`
	Requested    int            `json:"requested"`
	Succeeded    int            `json:"succeeded"`
	Failed       int            `json:"failed"`
	SuccessIDs   []string       `json:"success_ids"`
	Failures     []BatchFailure `json:"failures"`
	AllSucceeded bool           `json:"all_succeeded"`
}

func (w *Writer) BatchTransition(ctx context.Context, ids []string, transfer bool, reason, operator string) (BatchResult, error) {
	result := BatchResult{Action: "close", Requested: len(ids), SuccessIDs: []string{}, Failures: []BatchFailure{}}
	target, content := StatusClosed, "批量关闭工单"
	if reason != "" {
		content = "批量关闭：" + reason
	}
	if transfer {
		result.Action, target, content = "transfer", StatusTransferredHuman, "转人工"
		if reason != "" {
			content = "转人工：" + reason
		} else {
			reason = "客服手动转人工"
		}
	}

	tx, err := w.db.BeginTx(ctx, nil)
	if err != nil {
		return result, err
	}
	defer tx.Rollback()
	for _, id := range ids {
		ticket, err := loadForWrite(ctx, tx, id)
		if errors.Is(err, sql.ErrNoRows) {
			result.Failures = append(result.Failures, BatchFailure{TicketID: id, Code: CodeNotFound, Reason: "工单不存在"})
			continue
		}
		if err != nil {
			return result, err
		}
		reply := map[string]any{"replier": systemReplier, "content": content, "at": nowSeconds(), "batch": true}
		if transfer {
			reply["kind"], reply["reason"], reply["reason_codes"], reply["operator"] = "transfer", reason, []string{}, operator
		}
		err = applyTransition(ctx, tx, ticket, target, operator, reply)
		var transition *TransitionError
		if errors.As(err, &transition) {
			result.Failures = append(result.Failures, BatchFailure{id, transition.Code, transition.Message, ticket.Status})
			continue
		}
		if err != nil {
			return result, err
		}
		result.SuccessIDs = append(result.SuccessIDs, id)
	}
	result.Succeeded, result.Failed = len(result.SuccessIDs), len(result.Failures)
	result.AllSucceeded = result.Failed == 0
	return result, tx.Commit()
}
