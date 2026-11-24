package tickets

import (
	"errors"
	"strings"
	"testing"
)

// allStatuses 是全部合法状态，顺序与 Python TicketStatus 声明一致。
// 反向测试要枚举所有两两组合，固定顺序能让失败输出稳定可读。
var allStatuses = []Status{
	StatusPendingConfirm,
	StatusOpen,
	StatusInProgress,
	StatusResolved,
	StatusTransferredHuman,
	StatusClosed,
}

// TestValidateTransitionAllowsEveryListedEdge 正向遍历流转表，
// 保证表里写下的每一条边都能通过校验——避免「表加了但校验没跟上」。
func TestValidateTransitionAllowsEveryListedEdge(t *testing.T) {
	for from, targets := range validTransitions {
		for _, to := range targets {
			if err := ValidateTransition(string(from), string(to)); err != nil {
				t.Errorf("%s -> %s 应为合法流转，实际报错: %v", from, to, err)
			}
		}
	}
}

// TestValidateTransitionRejectsEveryUnlistedPair 枚举 6×6 全部组合，
// 把「未出现在表里的一律拒绝」这条规则钉死。
//
// 只测具体几条非法边是不够的：新增状态时最容易漏的是「新状态没有任何出口」
// 或「旧状态忘了加新出口」，枚举式断言能同时覆盖这两种遗漏。
func TestValidateTransitionRejectsEveryUnlistedPair(t *testing.T) {
	for _, from := range allStatuses {
		for _, to := range allStatuses {
			allowed := canTransition(from, to)
			err := ValidateTransition(string(from), string(to))

			if allowed {
				if err != nil {
					t.Errorf("%s -> %s 在流转表内，不应被拒绝: %v", from, to, err)
				}
				continue
			}

			var te *TransitionError
			if !errors.As(err, &te) {
				t.Errorf("%s -> %s 应被拒绝，实际 err=%v", from, to, err)
				continue
			}
			if te.Code != CodeInvalidTransition {
				t.Errorf("%s -> %s 错误码应为 %s，实际 %s", from, to, CodeInvalidTransition, te.Code)
			}
			if te.From != string(from) || te.To != string(to) {
				t.Errorf("%s -> %s 错误详情应原样回显，实际 from=%q to=%q", from, to, te.From, te.To)
			}
		}
	}
}

// TestTransitionTableHasNoSelfLoops 单独锁住「同状态不算合法流转」。
//
// 这条对 update_status 尤其关键：把 closed 再置为 closed 必须失败，
// 否则「重复调用」会被静默当成成功，掩盖掉调用方的逻辑错误。
func TestTransitionTableHasNoSelfLoops(t *testing.T) {
	for from, targets := range validTransitions {
		for _, to := range targets {
			if from == to {
				t.Errorf("%s 不应允许流转到自身", from)
			}
		}
	}
}

func TestValidateTransitionUnknownStatus(t *testing.T) {
	cases := []struct {
		name        string
		current     string
		target      string
		wantMessage string
	}{
		{"当前状态未知", "archived", "open", "未知的当前状态：archived"},
		{"当前状态为空", "", "open", "未知的当前状态：空值"},
		{"目标状态未知", "open", "archived", "未知的目标状态：archived"},
		{"目标状态为空", "open", "", "未知的目标状态：空值"},
		// 两者都未知时必须先报当前状态：先定位「从哪来」才能判断
		// 是数据脏了还是调用方传错了，报目标状态会误导排查方向。
		{"两者都未知时报当前", "archived", "archived2", "未知的当前状态：archived"},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := ValidateTransition(c.current, c.target)
			var te *TransitionError
			if !errors.As(err, &te) {
				t.Fatalf("应为 *TransitionError，实际 %v", err)
			}
			if te.Code != CodeUnknownStatus {
				t.Errorf("错误码应为 %s，实际 %s", CodeUnknownStatus, te.Code)
			}
			if te.Message != c.wantMessage {
				t.Errorf("文案应为 %q，实际 %q", c.wantMessage, te.Message)
			}
		})
	}
}

func TestValidateTransitionInvalidMessage(t *testing.T) {
	err := ValidateTransition("closed", "open")
	var te *TransitionError
	if !errors.As(err, &te) {
		t.Fatalf("应为 *TransitionError，实际 %v", err)
	}
	if te.Message != "非法状态流转：closed -> open" {
		t.Errorf("文案不符: %q", te.Message)
	}
	if !strings.Contains(te.Error(), "closed -> open") {
		t.Errorf("Error() 应包含流转详情: %q", te.Error())
	}
}

func TestIsValidStatus(t *testing.T) {
	for _, v := range []string{
		"open", "in_progress", "resolved", "pending_confirm", "transferred_human", "closed",
	} {
		if !IsValidStatus(v) {
			t.Errorf("%q 应视为合法状态", v)
		}
	}

	// 大小写与首尾空白都必须是非法值：状态值直接参与 SQL 比较，
	// 做宽容归一化会让「库里是 open、入参是 OPEN」这种脏数据静默通过。
	for _, v := range []string{"", " ", "OPEN", " open", "open ", "archived", "pending", "done"} {
		if IsValidStatus(v) {
			t.Errorf("%q 不应视为合法状态", v)
		}
	}
}
