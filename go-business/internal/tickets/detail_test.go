package tickets

import (
	"context"
	"errors"
	"testing"
)

func TestDetailPreservesLegacyRepliesAndScopesOwner(t *testing.T) {
	_, db := newTestWriter(t)
	seedTicket(t, db, "T-1", string(StatusOpen), `[
		"  旧版系统说明  ",
		{"author":"客服","message":"请   重试","timestamp":"1700000120"},
		{"replier":"system","text":"转交","kind":"transfer","reason":"疑难问题","reason_codes":["user_requested"]}
	]`)
	_, err := db.Exec("UPDATE tickets SET tried_steps = ?, evidence = ? WHERE id = ?",
		"1. 重启 2. 换网络 · 重启", "规则通过（来源：产品手册）", "T-1")
	if err != nil {
		t.Fatal(err)
	}
	repo := NewRepository(db)
	if _, _, err := repo.Detail(context.Background(), "T-1", "u-other"); err == nil {
		t.Fatal("foreign owner was allowed access")
	} else {
		var ownership *OwnershipError
		if !errors.As(err, &ownership) || ownership.OwnerID != "u-1" {
			t.Fatalf("unexpected ownership error: %v", err)
		}
	}
	detail, _, err := repo.Detail(context.Background(), "T-1", "u-1")
	if err != nil {
		t.Fatal(err)
	}
	troubleshooting := detail["troubleshooting"].(map[string]any)
	if troubleshooting["tried_step_count"] != 2 ||
		troubleshooting["evidence"].([]map[string]any)[0]["source"] != "产品手册" {
		t.Fatalf("unexpected troubleshooting: %v", troubleshooting)
	}
	replies := detail["reply_timeline"].(replyTimeline)
	if replies.HumanCount != 1 || replies.SystemCount != 2 || replies.FirstResponseSeconds != 120 ||
		replies.LastTransferReason != "疑难问题" || replies.Items[1]["content"] != "请 重试" {
		t.Fatalf("unexpected replies: %+v", replies)
	}
}
