package tickets

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	_ "modernc.org/sqlite" // 测试自己开连接，需要在此处注册驱动
)

// newTestWriter 建一个临时 SQLite 库，表结构与 Alembic 迁移保持一致。
//
// 用临时文件而不是 :memory:：内存库是「每连接一个库」，一旦连接池建立
// 第二条连接就会看到空库；临时文件才复现真实的多连接行为。
// 连接池同样限制为 1，与 db.OpenWrite 的策略一致。
func newTestWriter(t *testing.T) (*Writer, *sql.DB) {
	t.Helper()

	path := filepath.Join(t.TempDir(), "tickets.db")
	dsn := fmt.Sprintf("file:%s?_pragma=busy_timeout(5000)&_txlock=immediate", path)
	conn, err := sql.Open("sqlite", dsn)
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	conn.SetMaxOpenConns(1)
	t.Cleanup(func() { conn.Close() })

	schema := []string{
		`CREATE TABLE ticket_number_counter (id INTEGER PRIMARY KEY, last_number BIGINT NOT NULL)`,
		`CREATE TABLE tried_steps (user_id TEXT NOT NULL, step TEXT NOT NULL, at REAL NOT NULL)`,
		`CREATE TABLE tickets (
			id TEXT PRIMARY KEY,
			dedup_key TEXT NOT NULL UNIQUE,
			user_id TEXT NOT NULL,
			user_name TEXT NOT NULL,
			description TEXT NOT NULL,
			tried_steps TEXT DEFAULT '',
			evidence TEXT DEFAULT '',
			status TEXT DEFAULT 'open',
			created_at REAL NOT NULL,
			replies TEXT DEFAULT '[]'
		)`,
		`CREATE TABLE ticket_status_logs (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			ticket_id TEXT NOT NULL,
			from_status TEXT DEFAULT '',
			to_status TEXT NOT NULL,
			operator TEXT DEFAULT 'system',
			changed_at REAL NOT NULL
		)`,
	}
	for _, stmt := range schema {
		if _, err := conn.Exec(stmt); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}

	return NewWriter(conn), conn
}

// seedTicket 插入一张工单，并补一条「当前状态的起始」时间线
// （from_status 为空，与 Python 建单时写的首条记录同形）。
func seedTicket(t *testing.T, db *sql.DB, id, status, repliesJSON string) {
	t.Helper()

	_, err := db.Exec(`
		INSERT INTO tickets (id, dedup_key, user_id, user_name, description, tried_steps, evidence, status, created_at, replies)
		VALUES (?, ?, 'u-1', '张三', '专业版导出失败', '', '', ?, 1700000000.0, ?)`,
		id, "dk-"+id, status, repliesJSON)
	if err != nil {
		t.Fatalf("插入工单失败: %v", err)
	}

	_, err = db.Exec(`
		INSERT INTO ticket_status_logs (ticket_id, from_status, to_status, operator, changed_at)
		VALUES (?, '', ?, 'system', 1700000000.0)`, id, status)
	if err != nil {
		t.Fatalf("插入初始时间线失败: %v", err)
	}
}

// transitionCount 统计真正发生过的流转条数（排除建单的起始记录）。
func transitionCount(t *testing.T, db *sql.DB, id string) int {
	t.Helper()
	var count int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM ticket_status_logs WHERE ticket_id = ? AND from_status <> ''`, id,
	).Scan(&count); err != nil {
		t.Fatalf("统计时间线失败: %v", err)
	}
	return count
}

func TestConfirmPendingTicket(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1001", string(StatusPendingConfirm), "[]")

	payload, err := writer.Confirm(context.Background(), "T-1001", "李四")
	if err != nil {
		t.Fatalf("确认应成功: %v", err)
	}
	if payload["status"] != string(StatusOpen) {
		t.Errorf("状态应为 open，实际 %v", payload["status"])
	}
	if got := transitionCount(t, db, "T-1001"); got != 1 {
		t.Errorf("应产生 1 条流转记录，实际 %d", got)
	}

	var from, to, operator string
	if err := db.QueryRow(`
		SELECT from_status, to_status, operator FROM ticket_status_logs
		WHERE ticket_id = ? AND from_status <> ''`, "T-1001",
	).Scan(&from, &to, &operator); err != nil {
		t.Fatalf("读取时间线失败: %v", err)
	}
	if from != string(StatusPendingConfirm) || to != string(StatusOpen) {
		t.Errorf("时间线应为 pending_confirm -> open，实际 %s -> %s", from, to)
	}
	if operator != "李四" {
		t.Errorf("操作人应为李四，实际 %q", operator)
	}
}

// TestConfirmIsIdempotent 重复确认不能产生第二次流转。
//
// 这是两阶段建单的核心语义：用户点两次确认、或前端超时重试，
// 都不该让工单多一条时间线、更不该报错。
func TestConfirmIsIdempotent(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1001", string(StatusPendingConfirm), "[]")

	first, err := writer.Confirm(context.Background(), "T-1001", "李四")
	if err != nil {
		t.Fatalf("首次确认失败: %v", err)
	}
	second, err := writer.Confirm(context.Background(), "T-1001", "李四")
	if err != nil {
		t.Fatalf("重复确认不应报错: %v", err)
	}

	if first["status"] != second["status"] {
		t.Errorf("两次返回状态应一致，实际 %v / %v", first["status"], second["status"])
	}
	if got := transitionCount(t, db, "T-1001"); got != 1 {
		t.Errorf("流转记录应保持 1 条，实际 %d", got)
	}
}

// TestConfirmOnNonPendingTicketReturnsAsIs 已关闭的工单再确认，
// 应原样返回而不是抛「非法流转」——幂等短路发生在状态机校验之前。
func TestConfirmOnNonPendingTicketReturnsAsIs(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1002", string(StatusClosed), "[]")

	payload, err := writer.Confirm(context.Background(), "T-1002", "李四")
	if err != nil {
		t.Fatalf("不应报错: %v", err)
	}
	if payload["status"] != string(StatusClosed) {
		t.Errorf("状态应保持 closed，实际 %v", payload["status"])
	}
	if got := transitionCount(t, db, "T-1002"); got != 0 {
		t.Errorf("不应产生流转记录，实际 %d", got)
	}
}

func TestConfirmMissingTicket(t *testing.T) {
	writer, _ := newTestWriter(t)

	for _, id := range []string{"T-9999", "", "   "} {
		payload, err := writer.Confirm(context.Background(), id, "李四")
		if !errors.Is(err, ErrNotFound) {
			t.Errorf("id=%q 应返回 ErrNotFound，实际 payload=%v err=%v", id, payload, err)
		}
	}
}

func TestCancelPendingTicketAppendsSystemReply(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1003", string(StatusPendingConfirm), "[]")

	payload, err := writer.Cancel(context.Background(), "T-1003", "问题已自行解决", "李四")
	if err != nil {
		t.Fatalf("取消应成功: %v", err)
	}
	if payload["status"] != string(StatusClosed) {
		t.Errorf("状态应为 closed，实际 %v", payload["status"])
	}

	replies, ok := payload["replies"].([]any)
	if !ok || len(replies) != 1 {
		t.Fatalf("应追加 1 条说明，实际 %#v", payload["replies"])
	}
	reply, ok := replies[0].(map[string]any)
	if !ok {
		t.Fatalf("回复项应为对象，实际 %#v", replies[0])
	}
	if reply["replier"] != systemReplier {
		t.Errorf("取消说明的署名应为 system，实际 %v", reply["replier"])
	}
	if reply["content"] != "取消工单：问题已自行解决" {
		t.Errorf("说明文案不符: %v", reply["content"])
	}
	if _, ok := reply["at"].(float64); !ok {
		t.Errorf("说明应带时间戳，实际 %#v", reply["at"])
	}

	// 落库的 replies 必须是合法 JSON，且与返回值一致
	var stored string
	if err := db.QueryRow(`SELECT replies FROM tickets WHERE id = ?`, "T-1003").Scan(&stored); err != nil {
		t.Fatalf("读取 replies 失败: %v", err)
	}
	if !strings.Contains(stored, "取消工单：问题已自行解决") {
		t.Errorf("落库内容不符: %s", stored)
	}
}

func TestCancelWithoutReason(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1004", string(StatusPendingConfirm), "[]")

	payload, err := writer.Cancel(context.Background(), "T-1004", "", "李四")
	if err != nil {
		t.Fatalf("取消应成功: %v", err)
	}
	replies := payload["replies"].([]any)
	reply := replies[0].(map[string]any)
	if reply["content"] != "取消工单：" {
		t.Errorf("无原因时文案应为「取消工单：」，实际 %v", reply["content"])
	}
}

func TestCancelIsIdempotent(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1005", string(StatusPendingConfirm), "[]")

	if _, err := writer.Cancel(context.Background(), "T-1005", "不要了", "李四"); err != nil {
		t.Fatalf("首次取消失败: %v", err)
	}
	// 已经 closed，第二次取消必须幂等返回，不能再追加一条说明
	payload, err := writer.Cancel(context.Background(), "T-1005", "不要了", "李四")
	if err != nil {
		t.Fatalf("重复取消不应报错: %v", err)
	}
	if replies := payload["replies"].([]any); len(replies) != 1 {
		t.Errorf("重复取消不应追加说明，实际 %d 条", len(replies))
	}
	if got := transitionCount(t, db, "T-1005"); got != 1 {
		t.Errorf("流转记录应保持 1 条，实际 %d", got)
	}
}

// TestCancelKeepsExistingReplies 取消发生在已有回复之后时，
// 原有回复必须保留——覆盖「append 时没先读旧值」这类典型错误。
func TestCancelKeepsExistingReplies(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1006", string(StatusPendingConfirm),
		`[{"replier":"客服A","content":"已收到","at":1700000100.0}]`)

	payload, err := writer.Cancel(context.Background(), "T-1006", "问题已解决", "李四")
	if err != nil {
		t.Fatalf("取消应成功: %v", err)
	}
	replies := payload["replies"].([]any)
	if len(replies) != 2 {
		t.Fatalf("应有 2 条回复，实际 %d", len(replies))
	}
	first := replies[0].(map[string]any)
	if first["replier"] != "客服A" || first["content"] != "已收到" {
		t.Errorf("原有回复被改动: %#v", first)
	}
}

func TestUpdateStatusLegalPath(t *testing.T) {
	cases := []struct {
		name string
		from Status
		to   Status
	}{
		{"待处理到处理中", StatusOpen, StatusInProgress},
		{"待处理到已解决", StatusOpen, StatusResolved},
		{"待处理到已关闭", StatusOpen, StatusClosed},
		{"处理中到已解决", StatusInProgress, StatusResolved},
		{"处理中到转人工", StatusInProgress, StatusTransferredHuman},
		{"已解决到已关闭", StatusResolved, StatusClosed},
		{"转人工到处理中", StatusTransferredHuman, StatusInProgress},
		{"转人工到已解决", StatusTransferredHuman, StatusResolved},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			writer, db := newTestWriter(t)
			seedTicket(t, db, "T-2001", string(c.from), "[]")

			payload, err := writer.UpdateStatus(context.Background(), "T-2001", string(c.to))
			if err != nil {
				t.Fatalf("%s -> %s 应成功: %v", c.from, c.to, err)
			}
			if payload["status"] != string(c.to) {
				t.Errorf("状态应为 %s，实际 %v", c.to, payload["status"])
			}
			// update_status 的操作人固定是 system，不接受外部指定
			var operator string
			if err := db.QueryRow(`
				SELECT operator FROM ticket_status_logs WHERE ticket_id = ? AND from_status <> ''`,
				"T-2001").Scan(&operator); err != nil {
				t.Fatalf("读取操作人失败: %v", err)
			}
			if operator != DefaultOperator {
				t.Errorf("操作人应为 system，实际 %q", operator)
			}
		})
	}
}

// TestUpdateStatusRejectsInvalidTransition 非法流转必须报错，
// 且工单与时间线都保持原样——校验发生在任何写入之前。
func TestUpdateStatusRejectsInvalidTransition(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-2002", string(StatusClosed), "[]")

	payload, err := writer.UpdateStatus(context.Background(), "T-2002", string(StatusOpen))
	var te *TransitionError
	if !errors.As(err, &te) {
		t.Fatalf("应返回 *TransitionError，实际 payload=%v err=%v", payload, err)
	}
	if te.Code != CodeInvalidTransition {
		t.Errorf("错误码应为 %s，实际 %s", CodeInvalidTransition, te.Code)
	}
	if te.From != string(StatusClosed) || te.To != string(StatusOpen) {
		t.Errorf("详情不符: %s -> %s", te.From, te.To)
	}

	var status string
	if err := db.QueryRow(`SELECT status FROM tickets WHERE id = ?`, "T-2002").Scan(&status); err != nil {
		t.Fatalf("读取状态失败: %v", err)
	}
	if status != string(StatusClosed) {
		t.Errorf("非法流转不应改动状态，实际 %s", status)
	}
	if got := transitionCount(t, db, "T-2002"); got != 0 {
		t.Errorf("非法流转不应写时间线，实际 %d 条", got)
	}
}

func TestUpdateStatusRejectsSameStatus(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-2003", string(StatusResolved), "[]")

	_, err := writer.UpdateStatus(context.Background(), "T-2003", string(StatusResolved))
	var te *TransitionError
	if !errors.As(err, &te) {
		t.Fatalf("同状态应被拒绝，实际 %v", err)
	}
	if te.Code != CodeInvalidTransition {
		t.Errorf("错误码应为 %s，实际 %s", CodeInvalidTransition, te.Code)
	}
}

func TestUpdateStatusRejectsUnknownStatus(t *testing.T) {
	for _, target := range []string{"archived", "", "OPEN"} {
		t.Run(fmt.Sprintf("target=%q", target), func(t *testing.T) {
			writer, db := newTestWriter(t)
			seedTicket(t, db, "T-2004", string(StatusOpen), "[]")

			_, err := writer.UpdateStatus(context.Background(), "T-2004", target)
			var te *TransitionError
			if !errors.As(err, &te) {
				t.Fatalf("未知状态应被拒绝，实际 %v", err)
			}
			if te.Code != CodeUnknownStatus {
				t.Errorf("错误码应为 %s，实际 %s", CodeUnknownStatus, te.Code)
			}
		})
	}
}

func TestUpdateStatusMissingTicket(t *testing.T) {
	writer, _ := newTestWriter(t)

	if _, err := writer.UpdateStatus(context.Background(), "T-9999", string(StatusResolved)); !errors.Is(err, ErrNotFound) {
		t.Fatalf("应返回 ErrNotFound，实际 %v", err)
	}
}

// TestConcurrentConfirmsOnlyTransitionOnce 并发确认只允许一次真实流转。
//
// 这条直接对应两阶段建单的并发风险：多个标签页同时点确认，
// 若无条件加锁与幂等判断，会写出多条时间线，甚至把已关闭的工单又打开。
func TestConcurrentConfirmsOnlyTransitionOnce(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-3001", string(StatusPendingConfirm), "[]")

	const workers = 12
	var wg sync.WaitGroup
	failures := make(chan error, workers)

	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			payload, err := writer.Confirm(context.Background(), "T-3001", "李四")
			if err != nil {
				failures <- err
				return
			}
			if payload["status"] != string(StatusOpen) {
				failures <- fmt.Errorf("并发确认后状态应为 open，实际 %v", payload["status"])
			}
		}()
	}
	wg.Wait()
	close(failures)

	for err := range failures {
		t.Errorf("并发确认失败: %v", err)
	}
	if got := transitionCount(t, db, "T-3001"); got != 1 {
		t.Errorf("并发下应只产生 1 条流转记录，实际 %d", got)
	}
}

func TestPayloadMatchesPythonTicketDict(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-4001", string(StatusPendingConfirm), "[]")

	payload, err := writer.Confirm(context.Background(), "T-4001", "李四")
	if err != nil {
		t.Fatalf("确认失败: %v", err)
	}

	// 与 backend/models/ticket.py 的 Ticket.to_dict() 逐键对齐：
	// 少一个字段前端就少渲染一块，多一个字段则说明两边契约已经开始分叉。
	want := []string{
		"id", "dedup_key", "user_id", "user_name", "description",
		"tried_steps", "evidence", "status", "created_at", "replies",
	}
	for _, key := range want {
		if _, ok := payload[key]; !ok {
			t.Errorf("payload 缺少字段 %q", key)
		}
	}
	if len(payload) != len(want) {
		t.Errorf("payload 字段数应为 %d，实际 %d: %#v", len(want), len(payload), payload)
	}
}

func TestCleanOperator(t *testing.T) {
	cases := []struct {
		name  string
		input string
		want  string
	}{
		{"空值回落兜底", "", DefaultOperator},
		{"纯空白回落兜底", "   \t\n ", DefaultOperator},
		{"压缩连续空白", "  李   四  ", "李 四"},
		{"正常名称原样保留", "客服小张", "客服小张"},
		{"超长按字符截断", strings.Repeat("中", 40), strings.Repeat("中", 32)},
		{"恰好上限不截断", strings.Repeat("a", 32), strings.Repeat("a", 32)},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := CleanOperator(c.input); got != c.want {
				t.Errorf("CleanOperator(%q) = %q，期望 %q", c.input, got, c.want)
			}
		})
	}
}
