package tickets

import (
	"database/sql"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

// newTestRepo 用临时文件建一张最小 tickets 表。用文件而不是 :memory:，
// 避免 database/sql 换连接时拿到另一份空的内存库。
func newTestRepo(t *testing.T) (*Repository, *sql.DB) {
	t.Helper()

	path := filepath.Join(t.TempDir(), "tickets.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	_, err = db.Exec(`
		CREATE TABLE tickets (
			id TEXT PRIMARY KEY,
			dedup_key TEXT,
			user_id TEXT,
			user_name TEXT,
			description TEXT,
			tried_steps TEXT,
			evidence TEXT,
			status TEXT,
			created_at REAL,
			replies TEXT
		)`)
	if err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	return NewRepository(db), db
}

func insertTicket(t *testing.T, db *sql.DB, id, userID, status string, createdAt float64, replies string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO tickets
		 (id, dedup_key, user_id, user_name, description, tried_steps, evidence, status, created_at, replies)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, "dedup-"+id, userID, "用户"+id, "导出失败", "重启客户端", "来自手册", status, createdAt, replies,
	)
	if err != nil {
		t.Fatalf("插入工单 %s 失败: %v", id, err)
	}
}

func itemIDs(result map[string]any) []string {
	items := result["items"].([]map[string]any)
	ids := make([]string, 0, len(items))
	for _, item := range items {
		ids = append(ids, item["id"].(string))
	}
	return ids
}

func TestListPendingFiltersExpiredAndOtherStatuses(t *testing.T) {
	repo, db := newTestRepo(t)
	now := 1_700_000_000.0

	insertTicket(t, db, "T-FRESH", "u1", PendingConfirm, now-60, `[]`)
	insertTicket(t, db, "T-OLD", "u1", PendingConfirm, now-float64(PendingTimeoutSeconds)-10, `[]`)
	insertTicket(t, db, "T-OPEN", "u1", "open", now-10, `[]`)

	result, err := repo.ListPending("", false, nil, &now)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}

	if got := itemIDs(result); len(got) != 1 || got[0] != "T-FRESH" {
		t.Fatalf("默认只应返回未过期的待确认草稿，得到 %v", got)
	}
	if result["total"] != 1 || result["expired"] != 0 {
		t.Fatalf("统计不符: total=%v expired=%v", result["total"], result["expired"])
	}
	if result["timeout_seconds"] != PendingTimeoutSeconds {
		t.Fatalf("默认阈值应为 %d，得到 %v", PendingTimeoutSeconds, result["timeout_seconds"])
	}
	if want := now - float64(PendingTimeoutSeconds); result["deadline"] != want {
		t.Fatalf("deadline 应为 %v，得到 %v", want, result["deadline"])
	}
}

func TestListPendingIncludeExpiredKeepsDescendingOrder(t *testing.T) {
	repo, db := newTestRepo(t)
	now := 1_700_000_000.0

	insertTicket(t, db, "T-FRESH", "u1", PendingConfirm, now-60, `[]`)
	insertTicket(t, db, "T-OLD", "u1", PendingConfirm, now-float64(PendingTimeoutSeconds)-10, `[]`)

	result, err := repo.ListPending("", true, nil, &now)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}

	if got := itemIDs(result); len(got) != 2 || got[0] != "T-FRESH" || got[1] != "T-OLD" {
		t.Fatalf("应按建单时间倒序返回两条，得到 %v", got)
	}
	if result["total"] != 2 || result["expired"] != 1 {
		t.Fatalf("统计不符: total=%v expired=%v", result["total"], result["expired"])
	}

	old := result["items"].([]map[string]any)[1]
	if old["expired"] != true || old["remaining_seconds"] != 0 {
		t.Fatalf("过期草稿应标记 expired 且剩余秒数归零，得到 %v", old)
	}
}

func TestListPendingScopesByUserID(t *testing.T) {
	repo, db := newTestRepo(t)
	now := 1_700_000_000.0

	insertTicket(t, db, "T-U1", "u1", PendingConfirm, now-10, `[]`)
	insertTicket(t, db, "T-U2", "u2", PendingConfirm, now-20, `[]`)

	result, err := repo.ListPending("u1", false, nil, &now)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}
	if got := itemIDs(result); len(got) != 1 || got[0] != "T-U1" {
		t.Fatalf("user_id 应限定数据可见范围，得到 %v", got)
	}

	all, err := repo.ListPending("", false, nil, &now)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}
	if got := itemIDs(all); len(got) != 2 {
		t.Fatalf("不传 user_id 应返回全量，得到 %v", got)
	}
}

func TestListPendingTimeoutVariants(t *testing.T) {
	repo, db := newTestRepo(t)
	now := 1_700_000_000.0
	insertTicket(t, db, "T-ONE", "u1", PendingConfirm, now-600, `[]`)

	cases := []struct {
		name           string
		timeout        int
		includeExpired bool
		wantItems      int
		wantExpired    int
		wantRemaining  int
		wantExpiresAt  float64
	}{
		{
			name:          "默认阈值内仍可确认",
			timeout:       PendingTimeoutSeconds,
			wantItems:     1,
			wantRemaining: PendingTimeoutSeconds - 600,
			wantExpiresAt: now - 600 + float64(PendingTimeoutSeconds),
		},
		{
			name:           "阈值 0 时未收敛的草稿也标记过期",
			timeout:        0,
			includeExpired: true,
			wantItems:      1,
			wantExpired:    1,
			wantRemaining:  0,
			wantExpiresAt:  now - 600,
		},
		{
			name:          "阈值过小且不包含过期时列表为空",
			timeout:       30,
			wantItems:     0,
			wantExpiresAt: now - 600 + 30,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			result, err := repo.ListPending("", c.includeExpired, &c.timeout, &now)
			if err != nil {
				t.Fatalf("查询失败: %v", err)
			}
			if got := len(result["items"].([]map[string]any)); got != c.wantItems {
				t.Fatalf("应有 %d 条，得到 %d", c.wantItems, got)
			}
			if result["expired"] != c.wantExpired {
				t.Fatalf("expired 应为 %d，得到 %v", c.wantExpired, result["expired"])
			}
			if c.wantItems == 0 {
				return
			}
			item := result["items"].([]map[string]any)[0]
			if item["remaining_seconds"] != c.wantRemaining {
				t.Fatalf("remaining_seconds 应为 %d，得到 %v", c.wantRemaining, item["remaining_seconds"])
			}
			if item["expires_at"] != c.wantExpiresAt {
				t.Fatalf("expires_at 应为 %v，得到 %v", c.wantExpiresAt, item["expires_at"])
			}
		})
	}
}

func TestListPendingRejectsNegativeTimeout(t *testing.T) {
	repo, _ := newTestRepo(t)
	negative := -1
	if _, err := repo.ListPending("", false, &negative, nil); err == nil {
		t.Fatal("负数阈值必须被拒绝，否则会把未到期草稿全部判成超时")
	}
}

func TestListPendingItemShapeMatchesPython(t *testing.T) {
	repo, db := newTestRepo(t)
	now := 1_700_000_000.0
	insertTicket(t, db, "T-SHAPE", "u1", PendingConfirm, now-10, `[{"replier":"system","content":"已创建"}]`)

	result, err := repo.ListPending("u1", false, nil, &now)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}
	item := result["items"].([]map[string]any)[0]

	// Python 的 Ticket.to_dict() 字段 + 列表补充的三个超时字段
	wantKeys := []string{
		"id", "dedup_key", "user_id", "user_name", "description",
		"tried_steps", "evidence", "status", "created_at", "replies",
		"expires_at", "remaining_seconds", "expired",
	}
	for _, key := range wantKeys {
		if _, ok := item[key]; !ok {
			t.Fatalf("缺少字段 %s，实际字段 %v", key, item)
		}
	}
	if item["status"] != PendingConfirm || item["user_id"] != "u1" {
		t.Fatalf("基础字段不符: %v", item)
	}
	replies, ok := item["replies"].([]any)
	if !ok || len(replies) != 1 {
		t.Fatalf("replies 应还原为一条 JSON 记录，得到 %v", item["replies"])
	}
	if replies[0].(map[string]any)["content"] != "已创建" {
		t.Fatalf("replies 内容不符: %v", replies[0])
	}
}
