package orders

import (
	"encoding/json"
	"reflect"
	"testing"
)

// pyScope 是 Python 的 evaluate_service_scope 在固定输入下的输出，
// 作为「行为一致」的基准。任何字段漂移都应让这里失败。
const (
	pyProInService = `{"allowed":true,"feature":{"code":"export.batch","included":true,"name":"批量导出","query":"批量导出"},"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":true,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":true,"known_tier":true,"order_id":"O1","order_status":"active","period":{"active":true,"days_remaining":180,"days_total":210,"days_used":30,"expired":false,"expires_at":1715552000.0,"expires_text":"2024-05-13","not_started":false,"percent_used":14.3,"started_at":1697408000.0,"started_text":"2023-10-16","status":"active","status_label":"服务中"},"reason":"在服务范围内","reason_code":"in_service","tier":"pro","tier_label":"专业版"}`

	pyBasicFeatureMissing = `{"allowed":false,"feature":{"code":"export.batch","included":false,"name":"批量导出","query":"批量导出"},"feature_codes":["export.basic","upload.single","support.community"],"feature_included":false,"features":["基础导出","单文件上传","社区支持"],"has_order":true,"in_service":true,"known_tier":true,"order_id":"O2","order_status":"active","period":{"active":true,"days_remaining":180,"days_total":210,"days_used":30,"expired":false,"expires_at":1715552000.0,"expires_text":"2024-05-13","not_started":false,"percent_used":14.3,"started_at":1697408000.0,"started_text":"2023-10-16","status":"active","status_label":"服务中"},"reason":"基础版当前版本不包含该功能「批量导出」","reason_code":"feature_not_included","tier":"basic","tier_label":"基础版"}`

	pyExpired = `{"allowed":false,"feature":{"code":"export.basic","included":true,"name":"基础导出","query":"导出"},"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":true,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":false,"known_tier":true,"order_id":"O3","order_status":"active","period":{"active":false,"days_remaining":0,"days_total":390,"days_used":390,"expired":true,"expires_at":1699136000.0,"expires_text":"2023-11-05","not_started":false,"percent_used":100.0,"started_at":1665440000.0,"started_text":"2022-10-11","status":"expired","status_label":"已到期"},"reason":"服务期已结束（2023-11-05）","reason_code":"expired","tier":"pro","tier_label":"专业版"}`

	pyNotStarted = `{"allowed":false,"feature":null,"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":true,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":false,"known_tier":true,"order_id":"O4","order_status":"active","period":{"active":false,"days_remaining":100,"days_total":90,"days_used":0,"expired":false,"expires_at":1708640000.0,"expires_text":"2024-02-23","not_started":true,"percent_used":0.0,"started_at":1700864000.0,"started_text":"2023-11-25","status":"not_started","status_label":"未开始"},"reason":"服务期尚未开始（2023-11-25 起）","reason_code":"not_started","tier":"pro","tier_label":"专业版"}`

	pyCancelled = `{"allowed":false,"feature":null,"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":true,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":false,"known_tier":true,"order_id":"O5","order_status":"cancelled","period":{"active":true,"days_remaining":180,"days_total":210,"days_used":30,"expired":false,"expires_at":1715552000.0,"expires_text":"2024-05-13","not_started":false,"percent_used":14.3,"started_at":1697408000.0,"started_text":"2023-10-16","status":"active","status_label":"服务中"},"reason":"订单当前不是有效状态，服务已停止","reason_code":"order_inactive","tier":"pro","tier_label":"专业版"}`

	pyUnknownTier = `{"allowed":false,"feature":null,"feature_codes":[],"feature_included":true,"features":[],"has_order":true,"in_service":true,"known_tier":false,"order_id":"O6","order_status":"active","period":{"active":true,"days_remaining":180,"days_total":210,"days_used":30,"expired":false,"expires_at":1715552000.0,"expires_text":"2024-05-13","not_started":false,"percent_used":14.3,"started_at":1697408000.0,"started_text":"2023-10-16","status":"active","status_label":"服务中"},"reason":"无法识别所购版本，需要人工核实购买情况","reason_code":"tier_unknown","tier":null,"tier_label":"未知版本"}`

	pyReversedPeriod = `{"allowed":false,"feature":null,"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":true,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":false,"known_tier":true,"order_id":"O7","order_status":"active","period":{"active":false,"days_remaining":0,"days_total":0,"days_used":0,"expired":false,"expires_at":1700864000.0,"expires_text":"2023-11-25","not_started":false,"percent_used":0.0,"started_at":1708640000.0,"started_text":"2024-02-23","status":"unknown","status_label":"无法判定"},"reason":"订单起止时间不完整，无法判断服务期","reason_code":"period_unknown","tier":"pro","tier_label":"专业版"}`

	pyFeatureUnknown = `{"allowed":false,"feature":{"code":"","included":false,"name":"","query":"区块链"},"feature_codes":["export.basic","upload.single","support.community","export.batch","report.advanced","support.priority","api.access"],"feature_included":false,"features":["基础导出","单文件上传","社区支持","批量导出","高级报表","优先客服支持","API 接口"],"has_order":true,"in_service":true,"known_tier":true,"order_id":"O8","order_status":"active","period":{"active":true,"days_remaining":180,"days_total":210,"days_used":30,"expired":false,"expires_at":1715552000.0,"expires_text":"2024-05-13","not_started":false,"percent_used":14.3,"started_at":1697408000.0,"started_text":"2023-10-16","status":"active","status_label":"服务中"},"reason":"无法识别要确认的功能「区块链」","reason_code":"feature_unknown","tier":"pro","tier_label":"专业版"}`

	pyNoOrder = `{"allowed":false,"feature":null,"feature_codes":[],"feature_included":false,"features":[],"has_order":false,"in_service":false,"known_tier":false,"order_id":null,"order_status":"","period":null,"reason":"未查询到订单记录，无法判断服务范围","reason_code":"no_order","tier":null,"tier_label":"未知版本"}`
)

func TestEvaluateServiceScopeMatchesPython(t *testing.T) {
	const base = 1700000000.0
	const day = 86400.0

	newOrder := func(id, tier, status string, start, end float64) *Order {
		return &Order{ID: id, UserID: "u-test", ProductTier: tier, Status: status, StartedAt: start, ExpiresAt: end}
	}

	cases := []struct {
		name    string
		order   *Order
		feature string
		now     float64
		want    string
	}{
		{"pro 在期+功能包含", newOrder("O1", "pro", "active", base-day*30, base+day*180), "批量导出", base, pyProInService},
		{"basic 在期+功能不包含", newOrder("O2", "basic", "active", base-day*30, base+day*180), "批量导出", base, pyBasicFeatureMissing},
		{"已过期", newOrder("O3", "pro", "active", base-day*400, base-day*10), "导出", base, pyExpired},
		{"未开始", newOrder("O4", "pro", "active", base+day*10, base+day*100), "", base, pyNotStarted},
		{"订单退订", newOrder("O5", "pro", "cancelled", base-day*30, base+day*180), "", base, pyCancelled},
		{"未知版本", newOrder("O6", "enterprise", "active", base-day*30, base+day*180), "", base, pyUnknownTier},
		{"起止颠倒", newOrder("O7", "pro", "active", base+day*100, base+day*10), "", base, pyReversedPeriod},
		{"功能无法识别", newOrder("O8", "pro", "active", base-day*30, base+day*180), "区块链", base, pyFeatureUnknown},
		{"无订单", nil, "", base, pyNoOrder},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := EvaluateServiceScope(c.order, c.feature, &c.now)

			gotJSON, err := json.Marshal(got)
			if err != nil {
				t.Fatalf("序列化失败: %v", err)
			}

			var gotMap, wantMap map[string]any
			if err := json.Unmarshal(gotJSON, &gotMap); err != nil {
				t.Fatalf("解析 Go 结果失败: %v", err)
			}
			if err := json.Unmarshal([]byte(c.want), &wantMap); err != nil {
				t.Fatalf("解析基准结果失败: %v", err)
			}

			if !reflect.DeepEqual(gotMap, wantMap) {
				for key := range wantMap {
					if !reflect.DeepEqual(gotMap[key], wantMap[key]) {
						t.Errorf("字段 %s 不一致：Go=%#v Python=%#v", key, gotMap[key], wantMap[key])
					}
				}
				t.Fatalf("与 Python 行为不一致；Go JSON: %s", gotJSON)
			}
		})
	}
}

func TestNormalizeTier(t *testing.T) {
	cases := map[string]struct {
		want   string
		wantOK bool
	}{
		"pro": {"pro", true}, "PRO": {"pro", true}, " 专业版 ": {"pro", true},
		"professional": {"pro", true}, "basic": {"basic", true}, "基础": {"basic", true},
		"standard": {"basic", true}, "enterprise": {"", false}, "": {"", false},
	}
	for input, want := range cases {
		got, ok := NormalizeTier(input)
		if got != want.want || ok != want.wantOK {
			t.Errorf("NormalizeTier(%q) = (%q,%v)，期望 (%q,%v)", input, got, ok, want.want, want.wantOK)
		}
	}
}

func TestResolveFeatureLongestMatch(t *testing.T) {
	cases := map[string]string{
		"批量导出失败": "export.batch", // 不能被更短的「导出」抢走
		"导出":     "export.basic",
		"普通导出":   "export.basic",
		"人工客服":   "support.priority",
		"api":    "api.access",
		"区块链":    "",
		"":       "",
	}
	for input, wantCode := range cases {
		got := ResolveFeature(input)
		gotCode := ""
		if got != nil {
			gotCode = got.Code
		}
		if gotCode != wantCode {
			t.Errorf("ResolveFeature(%q) = %q，期望 %q", input, gotCode, wantCode)
		}
	}
}

func TestHasCapability(t *testing.T) {
	cases := []struct {
		tier, feature string
		want          bool
	}{
		{"pro", "批量导出", true},
		{"basic", "批量导出", false},
		{"basic", "导出", true},
		{"pro", "区块链", false},
		{"enterprise", "导出", false},
	}
	for _, c := range cases {
		if got := HasCapability(c.tier, c.feature); got != c.want {
			t.Errorf("HasCapability(%q,%q) = %v，期望 %v", c.tier, c.feature, got, c.want)
		}
	}
}
