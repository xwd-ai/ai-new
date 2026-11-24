package orders

import (
	"database/sql"
	"encoding/json"
	"fmt"
)

// Repository 封装订单取数，SQL 与 Python 侧保持同一口径。
type Repository struct {
	db *sql.DB
}

// NewRepository 创建订单仓储。
func NewRepository(db *sql.DB) *Repository {
	return &Repository{db: db}
}

// List 取用户全部订单，按到期时间从晚到早排列，同到期时间按订单号升序。
//
// 把确定口径放在取数层，上层挑选与展示都不必再依赖数据库返回顺序。
func (r *Repository) List(userID string) ([]Order, error) {
	rows, err := r.db.Query(`
		SELECT id, user_id, product_tier, status, started_at, expires_at
		FROM orders
		WHERE user_id = ?
		ORDER BY expires_at DESC, id ASC
	`, userID)
	if err != nil {
		return nil, fmt.Errorf("查询订单失败: %w", err)
	}
	defer rows.Close()

	orders := make([]Order, 0, 4)
	for rows.Next() {
		var o Order
		if err := rows.Scan(&o.ID, &o.UserID, &o.ProductTier, &o.Status, &o.StartedAt, &o.ExpiresAt); err != nil {
			return nil, fmt.Errorf("解析订单失败: %w", err)
		}
		orders = append(orders, o)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("读取订单失败: %w", err)
	}
	return orders, nil
}

// GetOrder 取用户的一条订单：指定 orderID 时精确取，否则取主订单。
//
// 指定订单号但不属于该用户时返回 nil，由调用方按「订单不存在」处理 ——
// 别人的订单号不能因为格式正确就被放行。
func (r *Repository) GetOrder(userID, orderID string) (*Order, error) {
	orders, err := r.List(userID)
	if err != nil {
		return nil, err
	}
	if len(orders) == 0 {
		return nil, nil
	}
	if orderID != "" {
		for i := range orders {
			if orders[i].ID == orderID {
				return &orders[i], nil
			}
		}
		return nil, nil
	}
	return SelectPrimary(orders, nil), nil
}

// SelectPrimary 从多条订单中选出主订单（服务中优先 → 状态有效 → 到期更晚 → 单号更小）。
//
// 前三项仍不能区分时用订单号兜底，保证同一批数据每次挑出同一条；
// 否则「当前在用哪个版本」这类结论会在两次查询之间来回跳。
func SelectPrimary(orders []Order, now *float64) *Order {
	if len(orders) == 0 {
		return nil
	}
	best := &orders[0]
	for i := 1; i < len(orders); i++ {
		if rankLess(&orders[i], best, now) {
			best = &orders[i]
		}
	}
	return best
}

func rankLess(a, b *Order, now *float64) bool {
	sa := EvaluateServiceScope(a, "", now)
	sb := EvaluateServiceScope(b, "", now)

	// 服务中的排在前面
	ia, ib := 1, 1
	if sa.InService {
		ia = 0
	}
	if sb.InService {
		ib = 0
	}
	if ia != ib {
		return ia < ib
	}

	// 状态失效的排在后面（原因码为 order_inactive 表示被状态拦下）
	na, nb := 0, 0
	if sa.ReasonCode == "order_inactive" {
		na = 1
	}
	if sb.ReasonCode == "order_inactive" {
		nb = 1
	}
	if na != nb {
		return na < nb
	}

	// 到期更晚的优先（Python 对应 -expires 升序）
	ea, eb := expiresOf(sa), expiresOf(sb)
	if ea != eb {
		return ea > eb
	}
	return a.ID < b.ID
}

func expiresOf(scope Scope) float64 {
	if scope.Period != nil && scope.Period.ExpiresAt != nil {
		return *scope.Period.ExpiresAt
	}
	return 0
}

// ProductView 是版本的展示名与能力列表。
type ProductView struct {
	Name     string   `json:"name"`
	Features []string `json:"features"`
}

// ProductView 某版本的展示信息：优先取产品表配置（运营可改），
// 产品行缺失时退回内置能力矩阵，避免判定退化成「没有任何功能」。
func (r *Repository) ProductView(tier string) (ProductView, error) {
	key, ok := NormalizeTier(tier)
	if !ok {
		key = tier
	}

	var name, featuresJSON string
	err := r.db.QueryRow(`SELECT name, features FROM products WHERE id = ?`, key).
		Scan(&name, &featuresJSON)
	if err == nil {
		features := make([]string, 0, 8)
		if jsonErr := json.Unmarshal([]byte(featuresJSON), &features); jsonErr == nil {
			return ProductView{Name: name, Features: features}, nil
		}
	}
	if err != nil && err != sql.ErrNoRows {
		return ProductView{}, fmt.Errorf("查询产品失败: %w", err)
	}

	// 产品行不存在或 JSON 损坏：退回内置矩阵
	capabilities := TierCapabilities(tier)
	return ProductView{Name: capabilities.Label, Features: capabilities.Features}, nil
}

// QueryOrder 查询某用户的订单与服务范围，返回给 Agent 作为决策依据。
//
// 返回字段与 Python 的 orders.query_order 完全一致，调用方无需感知实现语言。
func (r *Repository) QueryOrder(userID string) (map[string]any, error) {
	order, err := r.GetOrder(userID, "")
	if err != nil {
		return nil, err
	}
	if order == nil {
		return map[string]any{
			"has_order": false,
			"message":   "未查询到该用户的订单记录",
		}, nil
	}

	scope := EvaluateServiceScope(order, "", nil)
	view, err := r.ProductView(order.ProductTier)
	if err != nil {
		return nil, err
	}

	return map[string]any{
		"has_order":    true,
		"order_id":     order.ID,
		"product_tier": order.ProductTier,
		"product_name": view.Name,
		"in_service":   scope.InService,
		"features":     view.Features,
		"expires_at":   order.ExpiresAt,
	}, nil
}

// QueryOrders 多订单视图：逐条摘要、主订单与在期版本的能力并集。
//
// 用户同时持多个在期订单时，可用功能取并集：基础版和专业版都在期，
// 不该因为主订单挑中基础版就把批量导出判成不可用。
func (r *Repository) QueryOrders(userID string, now *float64) (map[string]any, error) {
	orders, err := r.List(userID)
	if err != nil {
		return nil, err
	}
	if len(orders) == 0 {
		return map[string]any{
			"has_order":        false,
			"order_count":      0,
			"in_service_count": 0,
			"primary_order_id": nil,
			"orders":           []any{},
			"features":         []string{},
			"message":          "未查询到该用户的订单记录",
		}, nil
	}

	items := make([]map[string]any, 0, len(orders))
	activeCount := 0
	for i := range orders {
		scope := EvaluateServiceScope(&orders[i], "", now)
		items = append(items, map[string]any{
			"order_id":     orders[i].ID,
			"product_tier": orders[i].ProductTier,
			"tier":         scope.Tier,
			"tier_label":   scope.TierLabel,
			"order_status": scope.OrderStatus,
			"period":       scope.Period,
			"in_service":   scope.InService,
			"allowed":      scope.Allowed,
			"reason_code":  scope.ReasonCode,
			"reason":       scope.Reason,
		})
		if scope.InService {
			activeCount++
		}
	}

	primary := SelectPrimary(orders, now)

	// 在期版本的能力并集，按出现顺序去重
	features := make([]string, 0, 8)
	seen := make(map[string]bool, 8)
	for i := range orders {
		scope := EvaluateServiceScope(&orders[i], "", now)
		if !scope.InService {
			continue
		}
		tier := orders[i].ProductTier
		if scope.Tier != nil {
			tier = *scope.Tier
		}
		view, viewErr := r.ProductView(tier)
		if viewErr != nil {
			return nil, viewErr
		}
		for _, name := range view.Features {
			if !seen[name] {
				seen[name] = true
				features = append(features, name)
			}
		}
	}

	var message string
	switch {
	case activeCount == 0:
		message = fmt.Sprintf("共 %d 个订单，当前均不在服务期内", len(orders))
	case activeCount == len(orders):
		message = fmt.Sprintf("共 %d 个订单，均在服务期内", len(orders))
	default:
		message = fmt.Sprintf("共 %d 个订单，%d 个在服务期内", len(orders), activeCount)
	}

	var primaryID any
	if primary != nil {
		primaryID = primary.ID
	}

	return map[string]any{
		"has_order":        true,
		"order_count":      len(orders),
		"in_service_count": activeCount,
		"primary_order_id": primaryID,
		"orders":           items,
		"features":         features,
		"message":          message,
	}, nil
}
