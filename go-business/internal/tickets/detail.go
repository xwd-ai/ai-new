package tickets

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode"
)

var (
	troubleshootSeparator = regexp.MustCompile(`[\r\n]+|[;；]+|\s*[·•]\s*|\s+-\s+`)
	troubleshootPrefix    = regexp.MustCompile(`^\s*(?:[（(]?\d{1,2}[)）.、]|[①-⑳]|[-*•])\s*`)
	troubleshootNumber    = regexp.MustCompile(`[（(]?\d{1,2}[)）.、]|[①-⑳]`)
	evidenceSource        = regexp.MustCompile(`[（(【\[]\s*(?:来源|出处|依据)\s*[:：]?\s*([^）)】\]]+?)\s*[）)】\]]`)
)

func (r *Repository) Detail(ctx context.Context, id, owner string) (map[string]any, string, error) {
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, "", err
	}
	defer tx.Rollback()
	ticket, err := scanTicket(tx.QueryRowContext(ctx, ticketSelect+" WHERE id = ?", id))
	if err != nil {
		return nil, "", err
	}
	if owner != "" && ticket.UserID != owner {
		return nil, "", &OwnershipError{OwnerID: ticket.UserID}
	}
	logs, chain, last, err := readDetailTimeline(ctx, tx, id)
	if err != nil {
		return nil, "", err
	}
	steps, sources := troubleshootItems(ticket.TriedSteps, false), troubleshootItems(ticket.Evidence, true)
	replies := detailReplies(ticket.Replies, ticket.CreatedAt)
	result := ticket.payload()
	result["troubleshooting"] = map[string]any{
		"tried_steps": steps, "evidence": sources, "tried_step_count": len(steps), "evidence_count": len(sources),
		"has_tried_steps": len(steps) > 0, "has_evidence": len(sources) > 0,
	}
	result["status_timeline"] = logs
	result["reply_timeline"] = replies
	result["detail_summary"] = map[string]any{
		"tried_step_count": len(steps), "evidence_count": len(sources), "status_change_count": len(logs),
		"status_chain": chain, "last_changed_at": last, "reply_count": replies.Total, "human_reply_count": replies.HumanCount,
		"system_reply_count": replies.SystemCount, "has_human_reply": replies.HasHumanReply,
		"first_response_seconds": replies.FirstResponseSeconds, "transfer_count": replies.TransferCount,
	}
	troubleshooting := "暂无已尝试步骤与排查依据"
	if len(steps)+len(sources) > 0 {
		troubleshooting = fmt.Sprintf("留存 %d 条已尝试步骤、%d 条排查依据", len(steps), len(sources))
	}
	replyMessage := "暂无回复记录"
	if replies.HasReplies {
		replyMessage = fmt.Sprintf("共 %d 条回复（人工 %d 条、系统 %d 条）", replies.Total, replies.HumanCount, replies.SystemCount)
	}
	return result, fmt.Sprintf("工单 %s，%s，%s", id, troubleshooting, replyMessage), nil
}

func readDetailTimeline(ctx context.Context, tx *sql.Tx, id string) ([]map[string]any, []string, any, error) {
	rows, err := tx.QueryContext(ctx, `SELECT id, from_status, to_status, operator, changed_at
		FROM ticket_status_logs WHERE ticket_id = ? ORDER BY changed_at, id`, id)
	if err != nil {
		return nil, nil, nil, err
	}
	defer rows.Close()
	logs, chain := []map[string]any{}, []string{}
	var last any
	for rows.Next() {
		var number int
		var from, to, operator string
		var changed float64
		if err := rows.Scan(&number, &from, &to, &operator, &changed); err != nil {
			return nil, nil, nil, err
		}
		logs = append(logs, map[string]any{"id": number, "ticket_id": id, "from_status": from,
			"to_status": to, "operator": operator, "changed_at": changed})
		chain = append(chain, to)
		last = changed
	}
	return logs, chain, last, rows.Err()
}

func troubleshootItems(input string, evidence bool) []map[string]any {
	items := []map[string]any{}
	seen := map[string]bool{}
	for _, segment := range troubleshootSeparator.Split(input, -1) {
		for _, part := range numberedSegments(strings.TrimSpace(segment)) {
			source := ""
			if evidence {
				if match := evidenceSource.FindStringSubmatchIndex(part); match != nil {
					source = strings.TrimSpace(part[match[2]:match[3]])
					part = strings.Trim(part[:match[0]]+part[match[1]:], " \t，,；;")
				}
			}
			text := strings.Join(strings.Fields(troubleshootPrefix.ReplaceAllString(strings.TrimSpace(part), "")), " ")
			if runes := []rune(text); len(runes) > 200 {
				text = strings.TrimRightFunc(string(runes[:199]), unicode.IsSpace) + "…"
			}
			if text == "" || seen[text] {
				continue
			}
			seen[text] = true
			item := map[string]any{"index": len(items) + 1, "text": text}
			if evidence {
				item["source"] = source
			}
			items = append(items, item)
			if len(items) == 50 {
				return items
			}
		}
	}
	return items
}

func numberedSegments(text string) []string {
	indices := troubleshootNumber.FindAllStringIndex(text, -1)
	if len(indices) == 0 || indices[0][0] != 0 {
		return []string{strings.TrimSpace(troubleshootPrefix.ReplaceAllString(text, ""))}
	}
	parts := make([]string, 0, len(indices))
	for i, match := range indices {
		end := len(text)
		if i+1 < len(indices) {
			end = indices[i+1][0]
		}
		parts = append(parts, strings.TrimSpace(text[match[1]:end]))
	}
	return parts
}

type replyTimeline struct {
	Items                []map[string]any `json:"items"`
	Total                int              `json:"total"`
	HumanCount           int              `json:"human_count"`
	SystemCount          int              `json:"system_count"`
	HasReplies           bool             `json:"has_replies"`
	HasHumanReply        bool             `json:"has_human_reply"`
	FirstReplyAt         any              `json:"first_reply_at"`
	LastReplyAt          any              `json:"last_reply_at"`
	LastHumanReplyAt     any              `json:"last_human_reply_at"`
	FirstResponseSeconds any              `json:"first_response_seconds"`
	TransferCount        int              `json:"transfer_count"`
	LastTransferReason   string           `json:"last_transfer_reason"`
	Truncated            bool             `json:"truncated"`
}

func detailReplies(raw []any, createdAt float64) replyTimeline {
	result := replyTimeline{Items: []map[string]any{}}
	for _, value := range raw {
		if entry := normalizeDetailReply(value); entry != nil {
			result.Items = append(result.Items, entry)
		}
	}
	if len(result.Items) > 100 {
		result.Items = result.Items[len(result.Items)-100:]
		result.Truncated = true
	}
	for i, entry := range result.Items {
		entry["index"] = i + 1
		if i == 0 {
			result.FirstReplyAt = entry["at"]
		}
		result.LastReplyAt = entry["at"]
		if entry["is_human"] == true {
			if result.HumanCount == 0 && createdAt != 0 {
				if at, ok := entry["at"].(float64); ok {
					result.FirstResponseSeconds = int(math.RoundToEven(max(0, at-createdAt)))
				}
			}
			result.HumanCount++
			result.LastHumanReplyAt = entry["at"]
		}
		if entry["kind"] == "transfer" {
			result.TransferCount++
			result.LastTransferReason = entry["reason"].(string)
		}
	}
	result.Total = len(result.Items)
	result.SystemCount = result.Total - result.HumanCount
	result.HasReplies, result.HasHumanReply = result.Total > 0, result.HumanCount > 0
	return result
}

func normalizeDetailReply(value any) map[string]any {
	var content, name string
	var at any
	entry := map[string]any{}
	switch raw := value.(type) {
	case string:
		content = raw
	case map[string]any:
		name = replyText(firstReplyValue(raw, "replier", "author", "operator", "user_name", "name"))
		content = replyText(firstReplyValue(raw, "content", "text", "message"))
		at = firstReplyValue(raw, "at", "time", "created_at", "timestamp")
		if kind := strings.TrimSpace(replyText(raw["kind"])); kind != "" {
			codes := []string{}
			if values, ok := raw["reason_codes"].([]any); ok {
				for _, code := range values {
					codes = append(codes, replyText(code))
				}
			}
			entry["kind"], entry["reason_codes"] = kind, codes
			entry["reason"] = strings.TrimSpace(replyText(raw["reason"]))
			entry["operator"] = strings.TrimSpace(replyText(raw["operator"]))
		}
	default:
		return nil
	}
	content = strings.Join(strings.Fields(content), " ")
	if content == "" {
		return nil
	}
	name = strings.TrimSpace(name)
	if name == "" {
		name = systemReplier
	}
	source := "human"
	if name == systemReplier {
		source = "system"
	}
	entry["replier"], entry["content"], entry["source"], entry["is_human"] = name, content, source, source == "human"
	entry["at"], entry["at_text"] = nil, ""
	var timestamp float64
	switch raw := at.(type) {
	case string:
		timestamp, _ = strconv.ParseFloat(strings.TrimSpace(raw), 64)
	case float64:
		timestamp = raw
	}
	if timestamp > 0 && timestamp < 253402300800 {
		entry["at"] = timestamp
		entry["at_text"] = time.Unix(int64(timestamp), 0).In(time.Local).Format("2006-01-02T15:04:05")
	}
	return entry
}

func firstReplyValue(values map[string]any, keys ...string) any {
	for _, key := range keys {
		if value := values[key]; value != nil && value != "" {
			return value
		}
	}
	return nil
}

func replyText(value any) string {
	if value == nil || value == false || value == float64(0) {
		return ""
	}
	return fmt.Sprint(value)
}
