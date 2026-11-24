package orders

import (
	"math"
	"time"
)

// 服务期状态。只有 active 表示可提供服务。
const (
	ServiceActive     = "active"
	ServiceNotStarted = "not_started"
	ServiceExpired    = "expired"
	ServiceUnknown    = "unknown"

	daySeconds = 86400.0
)

var serviceStatusLabels = map[string]string{
	ServiceActive:     "服务中",
	ServiceNotStarted: "未开始",
	ServiceExpired:    "已到期",
	ServiceUnknown:    "无法判定",
}

// Period 是服务期判定结果，字段与 Python 实现一一对应。
type Period struct {
	Status        string   `json:"status"`
	StatusLabel   string   `json:"status_label"`
	StartedAt     *float64 `json:"started_at"`
	ExpiresAt     *float64 `json:"expires_at"`
	StartedText   string   `json:"started_text"`
	ExpiresText   string   `json:"expires_text"`
	DaysTotal     int      `json:"days_total"`
	DaysUsed      int      `json:"days_used"`
	DaysRemaining int      `json:"days_remaining"`
	PercentUsed   float64  `json:"percent_used"`
	Active        bool     `json:"active"`
	Expired       bool     `json:"expired"`
	NotStarted    bool     `json:"not_started"`
}

// nowSeconds 返回带小数的当前时间戳，与 Python 的 time.time() 同精度。
func nowSeconds() float64 {
	return float64(time.Now().UnixNano()) / 1e9
}

// EvaluateServicePeriod 判定服务期状态并给出天数口径。
//
// 边界左闭右开：开始当刻即算服务中，到期当刻即算已到期。
// 起止时间缺失或颠倒时返回 unknown 而不是硬算 —— 按已到期处理会误伤用户，
// 按服务中处理又会放开不该给的权益，交给人工核实更稳妥。
func EvaluateServicePeriod(startedAt, expiresAt *float64, now *float64) Period {
	current := nowSeconds()
	if now != nil {
		current = *now
	}

	unknown := func(start, end *float64) Period {
		p := Period{
			Status:      ServiceUnknown,
			StatusLabel: serviceStatusLabels[ServiceUnknown],
			StartedAt:   start,
			ExpiresAt:   end,
		}
		if start != nil {
			p.StartedText = formatDay(*start)
		}
		if end != nil {
			p.ExpiresText = formatDay(*end)
		}
		return p
	}

	if startedAt == nil || expiresAt == nil {
		return unknown(startedAt, expiresAt)
	}
	start, end := *startedAt, *expiresAt
	if end <= start {
		return unknown(startedAt, expiresAt)
	}

	totalSeconds := end - start

	var status string
	switch {
	case current < start:
		status = ServiceNotStarted
	case current >= end:
		status = ServiceExpired
	default:
		status = ServiceActive
	}

	// 已用天数向下取整、剩余天数向上取整：用户视角里不足一天也算还有一天
	daysUsed := 0
	if current > start {
		daysUsed = int(math.Floor((math.Min(current, end) - start) / daySeconds))
	}
	daysRemaining := 0
	if current < end {
		daysRemaining = int(math.Ceil((end - current) / daySeconds))
	}

	covered := math.Min(math.Max(current, start), end)
	percentUsed := roundHalfEven((covered-start)/totalSeconds*100, 1)

	return Period{
		Status:        status,
		StatusLabel:   serviceStatusLabels[status],
		StartedAt:     startedAt,
		ExpiresAt:     expiresAt,
		StartedText:   formatDay(start),
		ExpiresText:   formatDay(end),
		DaysTotal:     int(math.Floor(totalSeconds / daySeconds)),
		DaysUsed:      daysUsed,
		DaysRemaining: daysRemaining,
		PercentUsed:   percentUsed,
		Active:        status == ServiceActive,
		Expired:       status == ServiceExpired,
		NotStarted:    status == ServiceNotStarted,
	}
}

// businessZone 是展示型日期的业务时区：固定北京时间（UTC+8）。
//
// 不能用 time.Local：同一份判定跑在 UTC 容器和 +08 开发机上会给出不同日期，
// 与 Python 侧的 BUSINESS_TZ 必须保持一致。FixedZone 不依赖系统 tzdata，
// 精简镜像里也不会因为缺时区文件退化成 UTC。
var businessZone = time.FixedZone("CST", 8*3600)

// formatDay 把时间戳转成业务时区日期，仅用于展示。
func formatDay(timestamp float64) string {
	return time.Unix(int64(timestamp), 0).In(businessZone).Format("2006-01-02")
}

// roundHalfEven 复现 Python 的 round(x, digits)：**四舍六入五成双**。
//
// Go 的 math.Round 是「五远离零」，与 Python 在 .5 边界上结果不同；
// 判定结果要与 Python 逐字段一致，这里必须对齐舍入规则。
func roundHalfEven(value float64, digits int) float64 {
	scale := math.Pow(10, float64(digits))
	scaled := value * scale
	floor := math.Floor(scaled)
	diff := scaled - floor

	var rounded float64
	switch {
	case diff > 0.5:
		rounded = floor + 1
	case diff < 0.5:
		rounded = floor
	default:
		// 正好在 .5：取最近的偶数
		if math.Mod(floor, 2) == 0 {
			rounded = floor
		} else {
			rounded = floor + 1
		}
	}
	return rounded / scale
}
