package tickets

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// ErrNotFound 表示工单不存在，接口层据此返回 404。
//
// 用哨兵错误而不是返回 (nil, nil)：后者会让调用方忘记判空，
// 把「查不到」当成「成功但没数据」继续往下走。
var ErrNotFound = errors.New("工单不存在")

type OwnershipError struct {
	OwnerID string
}

func (e *OwnershipError) Error() string { return "无权限" }

const (
	// DefaultOperator 是没有真实操作人时的兜底名称，与 Python 一致。
	DefaultOperator = "system"
	// OperatorNameMaxLength 是操作人展示名上限，与 Python 一致：
	// 超长截断而不是拒绝，避免一个异常昵称把整次流转打回。
	OperatorNameMaxLength = 32
	// systemReplier 是系统说明在 replies 里的署名，与 Python _SYSTEM_REPLIER 一致。
	systemReplier = "system"
)

// Writer 负责任工单状态流转的写操作。
//
// 与 Repository 分开而不是复用同一个 *sql.DB：查询走只读连接（物理上写不了），
// 写入走读写连接。能力边界落在连接上，比靠代码约定更可靠。
type Writer struct {
	db *sql.DB
}

// NewWriter 创建工单写入器；db 必须是可写连接（db.OpenWrite 的返回值）。
func NewWriter(db *sql.DB) *Writer {
	return &Writer{db: db}
}

// CleanOperator 归一化操作人名称，与 Python _clean_operator 同口径：
// 压掉连续空白、按字符截断超长、空值回落到兜底名称。
//
// 按 rune 截断而不是 byte：中文昵称一个字符占 3 字节，按字节切会切出
// 半个汉字，写进时间线就是乱码。
func CleanOperator(value string) string {
	fields := strings.Fields(value)
	if len(fields) == 0 {
		return DefaultOperator
	}
	joined := strings.Join(fields, " ")
	runes := []rune(joined)
	if len(runes) > OperatorNameMaxLength {
		return string(runes[:OperatorNameMaxLength])
	}
	return joined
}

// Confirm 确认待确认草稿，把它流转为待处理工单。
//
// 幂等：工单已不在 pending_confirm（已 open、已关闭、被并发确认）时
// 不报错、不重复流转，直接返回当前工单。重复确认是正常的用户行为
// （点两次、刷新后重试），不该让用户看到错误。
func (w *Writer) Confirm(ctx context.Context, ticketID, operator string) (map[string]any, error) {
	return w.ConfirmForOwner(ctx, ticketID, operator, "")
}

func (w *Writer) ConfirmForOwner(ctx context.Context, ticketID, operator, ownerID string) (map[string]any, error) {
	return w.transitionTicket(ctx, ticketID, StatusOpen, CleanOperator(operator), nil, true, ownerID)
}

// Cancel 取消待确认草稿，流转为已关闭，并把取消原因以系统身份记进 replies。
//
// 取消说明的署名固定是 system 而不是真实操作人：这条是系统生成的解释文本，
// 不该在时间线上算成一次「人工回复」，操作人另落在状态时间线里。
func (w *Writer) Cancel(ctx context.Context, ticketID, reason, operator string) (map[string]any, error) {
	return w.CancelForOwner(ctx, ticketID, reason, operator, "")
}

func (w *Writer) CancelForOwner(ctx context.Context, ticketID, reason, operator, ownerID string) (map[string]any, error) {
	reply := map[string]any{
		"replier": systemReplier,
		"content": strings.TrimSpace("取消工单：" + reason),
		"at":      nowSeconds(),
	}
	return w.transitionTicket(ctx, ticketID, StatusClosed, CleanOperator(operator), reply, true, ownerID)
}

// UpdateStatus 把工单流转到指定状态，操作人固定为 system。
//
// 不做幂等短路：目标与当前相同时会因「不在流转表内」被拒绝。
// 这与 confirm/cancel 不同，是刻意的——update_status 是管理端的显式动作，
// 传入一个「已经是该状态」的请求通常意味着调用方状态没刷新，
// 报错比静默成功更有用。
func (w *Writer) UpdateStatus(ctx context.Context, ticketID, status string) (map[string]any, error) {
	return w.transitionTicket(ctx, ticketID, Status(status), DefaultOperator, nil, false, "")
}

// transitionTicket 是三个写操作的共用骨架：一个事务里完成
// 「取当前记录 → 幂等判断 → 校验流转 → 写时间线 → 改状态」。
//
// idempotent=true 时，只要工单已不是 pending_confirm 就原样返回；
// false 时无条件走状态机校验。
func (w *Writer) transitionTicket(
	ctx context.Context,
	ticketID string,
	target Status,
	operator string,
	reply map[string]any,
	idempotent bool,
	ownerID string,
) (map[string]any, error) {
	if strings.TrimSpace(ticketID) == "" {
		return nil, ErrNotFound
	}

	tx, err := w.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("开启事务失败: %w", err)
	}
	// 未提交的事务一律回滚；成功路径显式 Commit 之后再 Rollback 是空操作，
	// 这样三条出口（异常、幂等返回、成功提交）都不需要各自记得清理。
	defer tx.Rollback()

	t, err := loadForWrite(ctx, tx, ticketID)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return nil, ErrNotFound
		}
		return nil, err
	}

	if ownerID != "" && t.UserID != ownerID {
		return nil, &OwnershipError{OwnerID: t.UserID}
	}

	if idempotent && Status(t.Status) != StatusPendingConfirm {
		return t.payload(), nil
	}

	if err := applyTransition(ctx, tx, t, target, operator, reply); err != nil {
		return nil, err
	}

	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("提交事务失败: %w", err)
	}
	return t.payload(), nil
}

// loadForWrite 在事务内读取完整工单。
//
// 与查询路径分开而不是复用 Repository：这里读的是即将被改写的那一行，
// 必须落在同一个写事务里，否则读到的是快照，写回去可能覆盖别人的修改。
func loadForWrite(ctx context.Context, tx *sql.Tx, id string) (*Ticket, error) {
	row := tx.QueryRowContext(ctx, `
		SELECT id, dedup_key, user_id, user_name, description, tried_steps, evidence, status, created_at, replies
		FROM tickets WHERE id = ?`, id)

	var t Ticket
	var rawReplies any
	if err := row.Scan(
		&t.ID, &t.DedupKey, &t.UserID, &t.UserName, &t.Description,
		&t.TriedSteps, &t.Evidence, &t.Status, &t.CreatedAt, &rawReplies,
	); err != nil {
		// 原样上抛 sql.ErrNoRows，由调用方判断「不存在」还是真正的查询失败
		if errors.Is(err, sql.ErrNoRows) {
			return nil, sql.ErrNoRows
		}
		return nil, fmt.Errorf("查询工单失败: %w", err)
	}
	t.Replies = decodeReplies(rawReplies)
	return &t, nil
}

// applyTransition 是状态机的唯一写入口：校验 → 追加说明 → 写时间线 → 改状态。
//
// 校验放在最前面：非法流转时工单必须保持原样，一个字段都不能改。
// 时间线与状态在同一事务内写入，保证「状态变了但没有留痕」不会发生。
func applyTransition(
	ctx context.Context,
	tx *sql.Tx,
	t *Ticket,
	target Status,
	operator string,
	reply map[string]any,
) error {
	if err := ValidateTransition(t.Status, string(target)); err != nil {
		return err
	}

	replies := t.Replies
	if reply != nil {
		replies = append(replies, reply)
	}
	encoded, err := encodeReplies(replies)
	if err != nil {
		return err
	}

	from := t.Status
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO ticket_status_logs (ticket_id, from_status, to_status, operator, changed_at)
		VALUES (?, ?, ?, ?, ?)`,
		t.ID, from, string(target), operator, nowSeconds(),
	); err != nil {
		return fmt.Errorf("写入状态时间线失败: %w", err)
	}

	if _, err := tx.ExecContext(ctx, `
		UPDATE tickets SET status = ?, replies = ? WHERE id = ?`,
		string(target), encoded, t.ID,
	); err != nil {
		return fmt.Errorf("更新工单状态失败: %w", err)
	}

	// 内存里的副本同步更新：调用方拿到的 payload 必须是提交后的状态
	t.Status = string(target)
	t.Replies = replies
	return nil
}

// payload 复刻 Python Ticket.to_dict() 的字段集合。
//
// 少一个字段、多一个字段都会让「Go 与 Python 结果一致」这条断言失效，
// 因此这里不图省事直接返回结构体，而是显式列出全部键。
func (t *Ticket) payload() map[string]any {
	return map[string]any{
		"id":          t.ID,
		"dedup_key":   t.DedupKey,
		"user_id":     t.UserID,
		"user_name":   t.UserName,
		"description": t.Description,
		"tried_steps": t.TriedSteps,
		"evidence":    t.Evidence,
		"status":      t.Status,
		"created_at":  t.CreatedAt,
		"replies":     t.Replies,
	}
}

// decodeReplies 把 replies 列解析成列表。
//
// 驱动返回的类型取决于列的实际存储：TEXT 给 string/[]byte，NULL 给 nil。
// 一律兜底成空切片而不是 nil——JSON 序列化后必须是 []，前端按数组渲染，
// null 会直接把列表组件打挂。
func decodeReplies(raw any) []any {
	empty := make([]any, 0, 4)

	var data []byte
	switch v := raw.(type) {
	case nil:
		return empty
	case []byte:
		data = v
	case string:
		data = []byte(v)
	default:
		return empty
	}
	if len(data) == 0 {
		return empty
	}

	var out []any
	if err := json.Unmarshal(data, &out); err != nil || out == nil {
		return empty
	}
	return out
}

// encodeReplies 把回复列表序列化回 JSON 文本。
//
// 关掉 HTML 转义：默认会把 < > & 变成 \u003c 之类，虽然 JSON 语义相同，
// 但写进库里肉眼不可读，排查时得先解码才知道用户说了什么。
func encodeReplies(replies []any) (string, error) {
	var buf bytes.Buffer
	encoder := json.NewEncoder(&buf)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(replies); err != nil {
		return "", fmt.Errorf("序列化回复失败: %w", err)
	}
	return strings.TrimSuffix(buf.String(), "\n"), nil
}
