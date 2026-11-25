package tickets

import (
	"context"
	"testing"
)

func TestBatchTransitionAndScopedStatistics(t *testing.T) {
	writer, db := newTestWriter(t)
	seedTicket(t, db, "T-1", string(StatusOpen), "[]")
	seedTicket(t, db, "T-2", string(StatusClosed), "[]")
	seedTicket(t, db, "T-3", string(StatusOpen), "[]")
	if _, err := db.Exec("UPDATE tickets SET user_id = 'u-other' WHERE id = 'T-3'"); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	result, err := writer.BatchTransition(ctx, []string{"T-1", "T-2", "missing"}, true, "", "客服")
	if err != nil || result.Succeeded != 1 || result.Failed != 2 ||
		result.Failures[0].Code != CodeInvalidTransition || result.Failures[1].Code != CodeNotFound {
		t.Fatalf("unexpected batch result: %+v, %v", result, err)
	}

	result, err = writer.BatchTransition(ctx, []string{"T-1"}, false, "处理完成", "客服")
	if err != nil || result.Succeeded != 1 {
		t.Fatalf("close after transfer: %+v, %v", result, err)
	}

	repo := NewRepository(db)
	items, count, err := repo.Page(ctx, ListFilter{UserID: "u-1", Page: 1, Size: 1, Direction: "asc"})
	if err != nil || count != 2 || len(items) != 1 || items[0]["id"] != "T-1" {
		t.Fatalf("unexpected scoped page: %v, %d, %v", items, count, err)
	}
	stats, _, err := repo.Statistics(ctx, "u-1", nil)
	if err != nil || stats["total"] != 2 {
		t.Fatalf("unexpected statistics: %v, %v", stats, err)
	}
	handling := stats["handling_duration"].(map[string]any)
	if handling["sample_size"] != 2 || stats["status_counts"].(map[Status]int)[StatusClosed] != 2 {
		t.Fatalf("unexpected duration statistics: %v", stats)
	}
}
