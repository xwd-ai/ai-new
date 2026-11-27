package tickets

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
)

func TestCreateUsesSharedCounterAndDeduplicates(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-9999", "open", "[]")
	seedTicket(t, db, "T-10000", "open", "[]")
	seedTicket(t, db, "T-legacy", "open", "[]")
	if _, err := db.Exec(`INSERT INTO tried_steps VALUES ('u1', '第二步', 2), ('u1', '第一步', 1), ('u2', '他人的步骤', 0)`); err != nil {
		t.Fatal(err)
	}
	input := CreateInput{UserID: "u1", UserName: "张三", Description: " 导出失败 "}
	var createdCount atomic.Int32
	var calls sync.WaitGroup
	for range 8 {
		calls.Go(func() {
			ticket, created, err := writer.Create(context.Background(), input)
			if err != nil {
				t.Error(err)
				return
			}
			if created {
				createdCount.Add(1)
			}
			if ticket["id"] != "T-10001" || ticket["status"] != "pending_confirm" ||
				ticket["tried_steps"] != "第一步 · 第二步" || ticket["description"] != "导出失败" {
				t.Errorf("unexpected ticket: %#v", ticket)
			}
		})
	}
	calls.Wait()
	if createdCount.Load() != 1 {
		t.Fatalf("expected one new draft, got %d", createdCount.Load())
	}

	var count int
	var operator string
	if err := db.QueryRow(`SELECT COUNT(*), MAX(operator) FROM ticket_status_logs
		WHERE ticket_id = 'T-10001' AND from_status = '' AND to_status = 'pending_confirm'`).Scan(&count, &operator); err != nil || count != 1 || operator != "张三" {
		t.Fatalf("unexpected initial timeline: count=%d operator=%q err=%v", count, operator, err)
	}
	// Simulate Python taking the next number on the same counter before a Go write.
	if _, err := db.Exec("UPDATE ticket_number_counter SET last_number = 10002 WHERE id = 1"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE tickets SET status = 'resolved' WHERE id = 'T-10001'"); err != nil {
		t.Fatal(err)
	}
	ticket, created, err := writer.Create(context.Background(), input)
	if err != nil || !created || ticket["id"] != "T-10003" {
		t.Fatalf("expected a new draft after resolution: %v %t %v", ticket, created, err)
	}
}

func TestCreateRollsBackNumberAndDraftOnTimelineFailure(t *testing.T) {
	writer, db := newTestWriter(t)
	if _, err := db.Exec(`CREATE TRIGGER fail_log BEFORE INSERT ON ticket_status_logs
		BEGIN SELECT RAISE(ABORT, 'timeline failure'); END`); err != nil {
		t.Fatal(err)
	}
	input := CreateInput{UserID: "u1", UserName: "客户", Description: "导出失败"}
	if _, _, err := writer.Create(context.Background(), input); err == nil {
		t.Fatal("expected timeline error")
	}
	for _, table := range []string{"tickets", "ticket_number_counter"} {
		var count int
		if err := db.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&count); err != nil || count != 0 {
			t.Fatalf("rollback of %s: count=%d err=%v", table, count, err)
		}
	}
}

func TestConversationTicketLinksPersistWithoutDuplicatingTickets(t *testing.T) {
	writer, conn := newTestWriter(t)
	if _, err := conn.Exec(`
		CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id TEXT NOT NULL);
		CREATE TABLE conversation_tickets (conversation_id TEXT, ticket_id TEXT, PRIMARY KEY (conversation_id, ticket_id));
		INSERT INTO conversations VALUES ('one', 'u1'), ('two', 'u1'), ('foreign', 'u2');
	`); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	input := CreateInput{UserID: "u1", UserName: "客户", Description: "导出失败", ConversationID: "one"}
	original, created, err := writer.Create(ctx, input)
	if err != nil || !created {
		t.Fatalf("create ticket: %v, %v", created, err)
	}
	input.ConversationID = "two"
	for range 2 {
		again, created, err := writer.Create(ctx, input)
		if err != nil || created || again["id"] != original["id"] {
			t.Fatalf("deduplicate and link: %v %v %v", again, created, err)
		}
	}
	input.ConversationID = "foreign"
	if _, _, err := writer.Create(ctx, input); err != ErrConversationNotFound {
		t.Fatalf("foreign conversation accepted: %v", err)
	}

	repo := &Repository{db: conn}
	for session, expected := range map[string]int{"one": 1, "two": 1, "foreign": 0} {
		filter := ListFilter{UserID: "u1", ConversationID: session, Page: 1, Size: 20}
		rows, total, err := repo.Page(ctx, filter)
		if err != nil || total != expected || len(rows) != expected {
			t.Fatalf("list %s: %v %d %v", session, rows, total, err)
		}
	}
}
