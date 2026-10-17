import time

from backend.database import SessionLocal
from backend.models import Order, Product

# 产品与订单持久化到 SQLite 数据库
# 订单是「服务范围判定」的依据：确认用户购买的是不是专业版、是否在服务期内

# 产品版本及其特性（首次启动时预置）
_SEED_PRODUCTS = [
    {
        "id": "basic",
        "name": "基础版",
        "features": ["基础导出", "单文件上传", "社区支持"],
    },
    {
        "id": "pro",
        "name": "专业版",
        "features": ["批量导出", "高级报表", "优先客服支持", "API 接口"],
    },
]

# 演示订单：李女士 = u-customer，买了专业版且在服务期内
_SEED_ORDERS = [
    {
        "id": "O-1001",
        "user_id": "u-customer",
        "product_tier": "pro",
        "status": "active",
        "started_at": time.time() - 90 * 86400,
        "expires_at": time.time() + 180 * 86400,  # 未来半年，在服务期内
    },
]


def seed_orders() -> None:
    """预置产品与演示订单（幂等：已有数据则跳过）。"""
    db = SessionLocal()
    try:
        if db.query(Product).first() is None:
            for p in _SEED_PRODUCTS:
                db.add(Product(id=p["id"], name=p["name"], features=p["features"]))
        if db.query(Order).first() is None:
            for o in _SEED_ORDERS:
                db.add(Order(**o))
        db.commit()
    finally:
        db.close()


def _get_product(db, tier: str) -> dict | None:
    p = db.query(Product).filter(Product.id == tier).first()
    return p.to_dict() if p else None


def get_order(user_id: str) -> dict | None:
    db = SessionLocal()
    try:
        o = db.query(Order).filter(Order.user_id == user_id).first()
        return o.to_dict() if o else None
    finally:
        db.close()


def query_order(user_id: str) -> dict:
    """查询某用户的订单与服务范围，返回给 Agent 作为决策依据。"""
    order = get_order(user_id)
    if not order:
        return {
            "has_order": False,
            "message": "未查询到该用户的订单记录",
        }

    db = SessionLocal()
    try:
        product = _get_product(db, order["product_tier"])
    finally:
        db.close()

    now = time.time()
    in_service = order["status"] == "active" and order["expires_at"] > now
    tier_name = product["name"] if product else order["product_tier"]
    features = product["features"] if product else []

    return {
        "has_order": True,
        "order_id": order["id"],
        "product_tier": order["product_tier"],
        "product_name": tier_name,
        "in_service": in_service,
        "features": features,
        "expires_at": order["expires_at"],
    }
