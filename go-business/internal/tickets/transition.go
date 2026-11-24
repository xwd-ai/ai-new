package tickets

import "fmt"

// Status 是工单状态值，与 Python 的 TicketStatus 枚举逐项对应。
//
// 用 string 而不是 int：状态值直接写进数据库、出现在接口响应和时间线里，
// 用字符串可以省掉一层编解码，也让日志与 SQL 查询结果直接可读。
type Status string

const (
	// StatusPendingConfirm 是两阶段建单中「草稿等待用户确认」的状态。
	StatusPendingConfirm Status = "pending_confirm"
	// StatusOpen 是正式工单的初始状态：用户确认草稿后进入。
	StatusOpen Status = "open"
	// StatusInProgress 表示客服已开始跟进。
	StatusInProgress Status = "in_progress"
	// StatusResolved 表示问题已闭环。
	StatusResolved Status = "resolved"
	// StatusTransferredHuman 表示超出服务范围或排查无效，已转人工。
	StatusTransferredHuman Status = "transferred_human"
	// StatusClosed 是终态：确认前取消、超时收敛都会走到这里。
	StatusClosed Status = "closed"
)

// validTransitions 是合法流转表，key 为当前状态，value 为允许的目标状态。
//
// 刻意用 slice 而不是 set：一张工单最多四个出口，线性扫描的代价可以忽略，
// 换来的是可以稳定遍历——测试能按表驱动覆盖每一条边，也能据此枚举
// 「所有未列出的组合」做反向断言。用 map 的话遍历顺序不确定，
// 失败用例的报错顺序每次都不一样。
var validTransitions = map[Status][]Status{
	StatusOpen: {
		StatusInProgress,
		StatusResolved,
		StatusTransferredHuman,
		StatusClosed,
	},
	StatusInProgress: {
		StatusResolved,
		StatusTransferredHuman,
		StatusClosed,
	},
	StatusResolved: {
		StatusClosed,
	},
	StatusPendingConfirm: {
		StatusOpen,
		StatusClosed,
	},
	StatusTransferredHuman: {
		StatusInProgress,
		StatusResolved,
		StatusClosed,
	},
	// 终态：没有任何合法出口，包括自身
	StatusClosed: {},
}

// ErrorCode 与 Python 的 TicketErrorCode 同值。
//
// 错误码是接口契约：Python 侧要把 Go 的业务错误翻译回同一套异常，
// 网关与前端也可能按错误码分支，所以两边必须字面一致。
type ErrorCode string

const (
	CodeNotFound          ErrorCode = "TICKET_NOT_FOUND"
	CodeInvalidTransition ErrorCode = "TICKET_INVALID_TRANSITION"
	CodeUnknownStatus     ErrorCode = "TICKET_UNKNOWN_STATUS"
	CodeInvalidPayload    ErrorCode = "TICKET_INVALID_PAYLOAD"
)

// TransitionError 表示状态机拒绝了这次流转。
//
// 分三类：未知的当前状态、未知的目标状态、表里没有这条边。
// 前两类是「入参有问题」（Python 侧映射 422），第三类是「业务上不允许」（409），
// 混在一起会让前端无法区分「我传错了」和「这个动作现在不能做」。
type TransitionError struct {
	Code    ErrorCode
	Message string
	// From / To 用 string 而不是 Status：未知状态下也要如实回显原始输入，
	// 转成 Status 就丢了「用户到底传了什么」这个最有用的排查信息。
	From string
	To   string
}

func (e *TransitionError) Error() string { return e.Message }

// IsValidStatus 判断字符串是否为合法状态值。
func IsValidStatus(value string) bool {
	_, ok := validTransitions[Status(value)]
	return ok
}

// ValidateTransition 校验流转是否合法，与 Python validate_transition 逐条对齐。
//
// 校验顺序刻意与 Python 保持一致：先当前状态、再目标状态、最后查表。
// 顺序会影响错误码——若先查表，「未知状态」会先命中「表里没有这条边」，
// 被报成 409 非法流转，而调用方真正需要知道的是「你传了个不存在的状态」。
func ValidateTransition(current, target string) error {
	if !IsValidStatus(current) {
		return &TransitionError{
			Code:    CodeUnknownStatus,
			Message: fmt.Sprintf("未知的当前状态：%s", nonEmptyText(current)),
			From:    current,
			To:      target,
		}
	}
	if !IsValidStatus(target) {
		return &TransitionError{
			Code:    CodeUnknownStatus,
			Message: fmt.Sprintf("未知的目标状态：%s", nonEmptyText(target)),
			From:    current,
			To:      target,
		}
	}
	if !canTransition(Status(current), Status(target)) {
		return &TransitionError{
			Code:    CodeInvalidTransition,
			Message: fmt.Sprintf("非法状态流转：%s -> %s", current, target),
			From:    current,
			To:      target,
		}
	}
	return nil
}

func canTransition(current, target Status) bool {
	for _, allowed := range validTransitions[current] {
		if allowed == target {
			return true
		}
	}
	return false
}

// nonEmptyText 把空值显示为「空值」：错误文案里出现一个空白字符，
// 调用方会以为是格式问题而不是漏传参数。
func nonEmptyText(value string) string {
	if value == "" {
		return "空值"
	}
	return value
}
