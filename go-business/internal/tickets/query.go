// Package tickets 是工单查询的 Go 实现。
//
// 迁移范围**只含只读**：待确认草稿列表与超时标记。
// 建单、确认、取消等写操作仍留在 Python —— 它们依赖唯一约束、事务与
// 分布式锁构成的三层幂等防护，重写风险远高于收益（写操作本身是低频）。
package tickets

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"time"
)

const (
	// PendingConfirm 是两阶段建单中「草稿等待用户确认」的状态值。
	PendingConfirm = "pending_confirm"
	// PendingTimeoutSeconds 与 Python 的 PENDING_CONFIRM_TIMEOUT_SECONDS 一致。
	PendingTimeoutSeconds = 30 * 60
)

// Ticket 对应 tickets 表。replies 是 JSON 数组，按原样透传。
type Ticket struct {
	ID          string
	DedupKey    string
	UserID      string
	UserName    string
	Description string
	TriedSteps  string
	Evidence    string
	Status      string
	CreatedAt   float64
	Replies     []any
}

// Repository 封装工单查询。
type Repository struct {
	db *sql.DB
}

// NewRepository 创建工单仓储。
func NewRepository(db *sql.DB) *Repository {
	return &Repository{db: db}
}

func nowSeconds() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

// ListPending 查询待确认草稿列表。
//
// 默认只返回仍可确认的草稿：超过超时阈值的草稿已被收敛任务关闭，不再出现；
// includeExpired=true 时一并返回，并以 expired / remaining_seconds 标记状态，
// 避免前端拿到无法确认的条目。按建单时间倒序，最新草稿排最前。
func (r *Repository) ListPending(userID string, includeExpired bool, timeoutSeconds *int, now *float64) (map[string]any, error) {
	timeout := PendingTimeoutSeconds
	if timeoutSeconds != nil {
		// 与 Python 的 _pending_timeout 同口径：负阈值会把还没到期的草稿
		// 全部判成超时，必须当场拒绝，而不是静默拉回 0 继续执行。
		if *timeoutSeconds < 0 {
			return nil, fmt.Errorf("待确认超时阈值不能为负数")
		}
		timeout = *timeoutSeconds
	}
	current := nowSeconds()
	if now != nil {
		current = *now
	}
	deadline := current - float64(timeout)

	query := `
		SELECT id, dedup_key, user_id, user_name, description, tried_steps, evidence, status, created_at, replies
		FROM tickets
		WHERE status = ?`
	args := []any{PendingConfirm}

	if userID != "" {
		query += " AND user_id = ?"
		args = append(args, userID)
	}
	if !includeExpired {
		// 建单时间晚于截止时间即为仍可确认的草稿
		query += " AND created_at > ?"
		args = append(args, deadline)
	}
	query += " ORDER BY created_at DESC"

	rows, err := r.db.Query(query, args...)
	if err != nil {
		return nil, fmt.Errorf("查询待确认工单失败: %w", err)
	}
	defer rows.Close()

	items := make([]map[string]any, 0, 8)
	expiredCount := 0

	for rows.Next() {
		var t Ticket
		var repliesJSON string
		if err := rows.Scan(
			&t.ID, &t.DedupKey, &t.UserID, &t.UserName, &t.Description,
			&t.TriedSteps, &t.Evidence, &t.Status, &t.CreatedAt, &repliesJSON,
		); err != nil {
			return nil, fmt.Errorf("解析工单失败: %w", err)
		}

		replies := make([]any, 0, 4)
		if repliesJSON != "" {
			_ = json.Unmarshal([]byte(repliesJSON), &replies)
		}

		// 确认截止时间与剩余秒数：前端据此展示倒计时或「已失效」
		expiresAt := t.CreatedAt + float64(timeout)
		remaining := expiresAt - current
		remainingSeconds := int(math.Max(0, remaining))
		expired := remaining <= 0
		if expired {
			expiredCount++
		}

		items = append(items, map[string]any{
			"id":                t.ID,
			"dedup_key":         t.DedupKey,
			"user_id":           t.UserID,
			"user_name":         t.UserName,
			"description":       t.Description,
			"tried_steps":       t.TriedSteps,
			"evidence":          t.Evidence,
			"status":            t.Status,
			"created_at":        t.CreatedAt,
			"replies":           replies,
			"expires_at":        expiresAt,
			"remaining_seconds": remainingSeconds,
			"expired":           expired,
		})
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("读取工单失败: %w", err)
	}

	return map[string]any{
		"items":           items,
		"total":           len(items),
		"expired":         expiredCount,
		"timeout_seconds": timeout,
		"deadline":        deadline,
	}, nil
}
