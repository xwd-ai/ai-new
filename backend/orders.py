import time

# 产品与订单数据（demo 阶段内置静态数据，不引数据库）
# 订单是「服务范围判定」的依据：确认用户购买的是不是专业版、是否在服务期内

# 产品版本及其特性
PRODUCTS = {
    "basic": {
        "name": "基础版",
        "features": ["基础导出", "单文件上传", "社区支持"],
    },
    "pro": {
        "name": "专业版",
        "features": ["批量导出", "高级报表", "优先客服支持", "API 接口"],
    },
}

# 演示订单
# 李女士 = u-customer，买了专业版且在服务期内
ORDERS = [
    {
        "id": "O-1001",
        "user_id": "u-customer",
        "product_tier": "pro",
        "status": "active",
        "started_at": time.time() - 90 * 86400,
        "expires_at": time.time() + 180 * 86400,  # 未来半年，在服务期内
    },
]


def get_order(user_id: str) -> dict | None:
    for o in ORDERS:
        if o["user_id"] == user_id:
            return o
    return None


def query_order(user_id: str) -> dict:
    """查询某用户的订单与服务范围，返回给 Agent 作为决策依据。"""
    order = get_order(user_id)
    if not order:
        return {
            "has_order": False,
            "message": "未查询到该用户的订单记录",
        }

    now = time.time()
    in_service = order["status"] == "active" and order["expires_at"] > now
    tier_name = PRODUCTS[order["product_tier"]]["name"]
    features = PRODUCTS[order["product_tier"]]["features"]

    return {
        "has_order": True,
        "order_id": order["id"],
        "product_tier": order["product_tier"],
        "product_name": tier_name,
        "in_service": in_service,
        "features": features,
        "expires_at": order["expires_at"],
    }
