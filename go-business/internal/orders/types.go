// Package orders 是订单与服务范围判定的 Go 实现，与 Python 侧
// backend/services/orders.py 保持行为一致。
//
// 迁移原则：**判定规则逐条对齐**，接口返回的字段名与语义不变，
// 使调用方（对话链路、工单接口）无需感知实现语言。
package orders

import "strings"

// Order 对应 orders 表。
type Order struct {
	ID          string  `json:"id"`
	UserID      string  `json:"user_id"`
	ProductTier string  `json:"product_tier"`
	Status      string  `json:"status"`
	StartedAt   float64 `json:"started_at"`
	ExpiresAt   float64 `json:"expires_at"`
}

// compact 把文本压成单行：换行与连续空白折成一个空格，空值收敛为空串。
//
// 与 Python 的 _compact 语义一致 —— 从对话或表单带过来的文本常夹带多余空白，
// 不归一就会出现「同一个订单号两种写法」。
func compact(value string) string {
	return strings.Join(strings.Fields(value), " ")
}
