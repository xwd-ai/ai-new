package tickets

import (
	"aibusiness/internal/db"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"

	"aibusiness/internal/orders"
)

var transferRequest = regexp.MustCompile(`转人工|人工客服|人工服务|人工处理|真人客服|找人工|找客服|联系客服|升级处理`)

type TransferInput struct {
	ID, OwnerID, UserText, Feature, Reason, Operator string
	Auto                                             bool
}

type TransferReason struct {
	Code   string `json:"code"`
	Label  string `json:"label"`
	Detail string `json:"detail"`
}

type TransferDecision struct {
	TicketID        string           `json:"ticket_id"`
	Status          string           `json:"status"`
	Applicable      bool             `json:"applicable"`
	ShouldTransfer  bool             `json:"should_transfer"`
	SuggestedStatus string           `json:"suggested_status"`
	ReasonCodes     []string         `json:"reason_codes"`
	Reasons         []TransferReason `json:"reasons"`
	ReasonText      string           `json:"reason_text"`
	Basis           map[string]any   `json:"basis"`
	Message         string           `json:"message"`
}

var ErrTransferRuleNotMatched = errors.New("未命中转人工规则")

func (r *Repository) TransferDecision(ctx context.Context, orderRepo *orders.Repository, input TransferInput) (TransferDecision, error) {
	ticket, err := scanTicket(r.db.QueryRowContext(ctx, ticketSelect+" WHERE id = ?", input.ID))
	if err != nil {
		return TransferDecision{}, err
	}
	if input.OwnerID != "" && ticket.UserID != input.OwnerID {
		return TransferDecision{}, &OwnershipError{OwnerID: ticket.UserID}
	}
	return evaluateTransfer(ticket, orderRepo, input)
}

func evaluateTransfer(ticket *Ticket, orderRepo *orders.Repository, input TransferInput) (TransferDecision, error) {
	rows, err := orderRepo.List(ticket.UserID)
	if err != nil {
		return TransferDecision{}, err
	}
	feature := strings.Join(strings.Fields(input.Feature), " ")
	scope := orders.EvaluateFeatureScope(rows, feature, nil)
	hasOrder := scope["has_order"].(bool)
	activeOrders := scope["in_service_count"].(int)
	scopeCode, scopeDetail := scope["reason_code"].(string), scope["reason"].(string)
	outside, unverified := false, false
	switch {
	case !hasOrder || activeOrders == 0:
		outside = true
	case feature != "" && scope["in_scope"] == false:
		switch scopeCode {
		case "tier_unknown", "period_unknown", "feature_unknown":
			unverified, scopeCode = true, "scope_unverified"
			scopeDetail = "无法自动核实服务范围：" + scopeDetail
		default:
			outside = true
		}
	default:
		scopeCode, scopeDetail = "in_scope", ""
	}
	tried := len(troubleshootItems(ticket.TriedSteps, false))
	human := detailReplies(ticket.Replies, ticket.CreatedAt).HumanCount
	decision := TransferDecision{TicketID: ticket.ID, Status: ticket.Status,
		Applicable:  canTransition(Status(ticket.Status), StatusTransferredHuman),
		ReasonCodes: []string{}, Reasons: []TransferReason{}}
	keyword := ""
	if decision.Applicable {
		keyword = transferRequest.FindString(input.UserText + "\n" + ticket.Description)
		if keyword != "" {
			decision.Reasons = append(decision.Reasons, TransferReason{"user_requested", "用户主动要求转人工", "命中「" + keyword + "」"})
		}
		switch {
		case outside:
			decision.Reasons = append(decision.Reasons, TransferReason{"out_of_scope", "超出服务范围", scopeDetail})
		case unverified:
			decision.Reasons = append(decision.Reasons, TransferReason{"scope_unverified", "服务范围待人工核实", scopeDetail})
		}
		switch {
		case tried >= 3:
			decision.Reasons = append(decision.Reasons, TransferReason{"ineffective_retries", "多次排查无效", fmt.Sprintf("已尝试 %d 步仍无结论", tried)})
		case human >= 2:
			decision.Reasons = append(decision.Reasons, TransferReason{"ineffective_retries", "多次排查无效", fmt.Sprintf("已有 %d 条人工回复仍未闭环", human)})
		}
	}
	var reasons []string
	for _, reason := range decision.Reasons {
		decision.ReasonCodes = append(decision.ReasonCodes, reason.Code)
		reasons = append(reasons, reason.Label+"（"+reason.Detail+"）")
	}
	decision.ReasonText = strings.Join(reasons, "；")
	decision.ShouldTransfer = len(decision.Reasons) > 0
	switch {
	case !decision.Applicable:
		decision.Message = fmt.Sprintf("工单当前状态为 %s，无需做转人工判定", ticket.Status)
	case decision.ShouldTransfer:
		decision.SuggestedStatus = string(StatusTransferredHuman)
		decision.Message = "建议转人工：" + decision.ReasonText
	default:
		decision.Message = "暂不需要转人工：问题在服务范围内，排查轮次也未达到阈值"
	}
	decision.Basis = map[string]any{
		"tried_step_count": tried, "tried_step_threshold": 3, "human_reply_count": human, "human_reply_threshold": 2,
		"feature": feature, "has_order": hasOrder, "in_service": activeOrders > 0, "matched_keyword": keyword,
		"service_scope": map[string]any{"out_of_scope": outside, "unverified": unverified, "code": scopeCode,
			"detail": scopeDetail, "order_count": scope["order_count"], "in_service_count": activeOrders, "covered_by": scope["covered_by"]},
	}
	return decision, nil
}

func (w *Writer) Transfer(ctx context.Context, orderRepo *orders.Repository, input TransferInput) (map[string]any, TransferDecision, error) {
	var decision TransferDecision
	tx, err := db.BeginWrite(ctx, w.db)
	if err != nil {
		return nil, decision, err
	}
	defer tx.Rollback()
	ticket, err := w.loadForWrite(ctx, tx, input.ID)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, decision, ErrNotFound
	}
	if err != nil {
		return nil, decision, err
	}
	decision, err = evaluateTransfer(ticket, orderRepo, input)
	if err != nil {
		return nil, decision, err
	}
	if input.Auto && !decision.ShouldTransfer {
		return nil, decision, ErrTransferRuleNotMatched
	}
	reason := strings.Join(strings.Fields(input.Reason), " ")
	if reason == "" {
		reason = strings.Join(strings.Fields(decision.ReasonText), " ")
	}
	if reason == "" {
		reason = "客服手动转人工"
	}
	from, when := ticket.Status, nowSeconds()
	reply := map[string]any{"replier": systemReplier, "content": "转人工：" + reason, "at": when, "kind": "transfer",
		"reason": reason, "reason_codes": decision.ReasonCodes, "operator": input.Operator}
	if err := applyTransition(ctx, tx, ticket, StatusTransferredHuman, input.Operator, reply); err != nil {
		return nil, decision, err
	}
	if err := tx.Commit(); err != nil {
		return nil, decision, err
	}
	result := ticket.payload()
	result["transfer"] = map[string]any{"from_status": from, "to_status": StatusTransferredHuman, "reason": reason,
		"reason_codes": decision.ReasonCodes, "operator": input.Operator, "at": when, "by_rule": decision.ShouldTransfer,
		"at_text": time.Unix(int64(when), 0).In(time.Local).Format("2006-01-02T15:04:05")}
	return result, decision, nil
}
