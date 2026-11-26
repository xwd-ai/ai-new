import hashlib
import os
import time

from backend.database import SessionLocal
from backend.models import KnowledgeDoc, Order, Product, Ticket, User
from backend.services.ticket_number import allocate_ticket_id

# 启动预置数据统一收敛在此模块。
# 各业务模块不再各自维护 seed 逻辑，进程启动时由 main 调用 seed_all() 一次完成预置。
# 所有预置均为幂等：对应表已有数据则跳过，不会重复写入。


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


_SEED_USERS = [
    {
        "id": "u-admin",
        "username": "admin",
        "password": "admin123",
        "name": "管理员",
        "role": "admin",
    },
    {
        "id": "u-customer",
        "username": "customer",
        "password": "customer123",
        "name": "李女士",
        "role": "customer",
    },
]

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
        "expires_at": time.time() + 180 * 86400,
    },
]

_SEED_KNOWLEDGE = [
    {
        "id": "k-export",
        "kind": "manual",
        "title": "产品手册《导出功能排查指南》",
        "keywords": ["导出", "失败", "export", "报错", "无法导出"],
        "content": (
            "导出功能常见失败原因与处理：\n"
            "1. 导出目录无写入权限 —— 检查目标文件夹是否只读，换一个有写权限的目录。\n"
            "2. 磁盘空间不足 —— 清理磁盘后重试。\n"
            "3. 客户端版本过旧 —— 升级到最新专业版客户端。\n"
            "4. 杀毒软件拦截 —— 将软件加入白名单后重试。\n"
            "5. 数据量过大 —— 专业版支持批量导出，基础版仅支持单文件导出。"
        ),
    },
    {
        "id": "k-activate",
        "kind": "faq",
        "title": "FAQ《激活码常见问题》",
        "keywords": ["激活", "激活码", "无效", "激活失败"],
        "content": (
            "激活码提示无效的处理：\n"
            "1. 确认激活码与购买版本一致（专业版/基础版激活码不通用）。\n"
            "2. 核对激活码大小写与连字符。\n"
            "3. 确认激活码未过期、未在其它设备使用超过授权数量。"
        ),
    },
    {
        "id": "k-refund",
        "kind": "faq",
        "title": "FAQ《退款申请流程》",
        "keywords": ["退款", "退费", "申请退款"],
        "content": (
            "退款申请流程：\n"
            "1. 购买 7 天内且未超过试用额度，可申请全额退款。\n"
            "2. 在「我的订单」中点击申请退款，填写原因。\n"
            "3. 客服审核通过后 3-5 个工作日原路退回。"
        ),
    },
    {
        "id": "k-service-rule",
        "kind": "rule",
        "title": "服务规则《售后支持范围》",
        "keywords": ["服务范围", "售后", "支持", "转人工", "专业版"],
        "content": (
            "售后支持范围：\n"
            "1. 专业版用户享受优先客服支持（服务期内）。\n"
            "2. 基础版用户仅提供社区支持。\n"
            "3. 服务期外的用户需续费后才能获得人工支持。\n"
            "4. 超出服务范围或多次排查无效的问题，转人工处理。"
        ),
    },
]

_SEED_TICKETS = [
    {
        "user_id": "u-customer",
        "user_name": "李女士",
        "description": "专业版导出功能一直失败",
        "tried_steps": "重启客户端 · 检查网络连接",
        "evidence": "产品手册《导出功能排查指南》",
    },
    {
        "user_id": "u-customer",
        "user_name": "王先生",
        "description": "激活码输入提示无效",
        "tried_steps": "核对邮箱中的激活码",
        "evidence": "FAQ《激活码常见问题》",
    },
]


def _seed_users(db) -> None:
    if db.query(User).first() is not None:
        return
    for u in _SEED_USERS:
        password = os.getenv(f"SEED_{u['role'].upper()}_PASSWORD") or u["password"]
        db.add(
            User(
                id=u["id"],
                username=u["username"],
                password_hash=_hash(password),
                name=u["name"],
                role=u["role"],
            )
        )


def _seed_products(db) -> None:
    if db.query(Product).first() is not None:
        return
    for p in _SEED_PRODUCTS:
        db.add(Product(id=p["id"], name=p["name"], features=p["features"]))


def _seed_orders(db) -> None:
    if db.query(Order).first() is not None:
        return
    for o in _SEED_ORDERS:
        db.add(Order(**o))


def _seed_knowledge(db) -> None:
    if db.query(KnowledgeDoc).first() is not None:
        return
    for item in _SEED_KNOWLEDGE:
        db.add(
            KnowledgeDoc(
                id=item["id"],
                kind=item["kind"],
                title=item["title"],
                keywords=item["keywords"],
                content=item["content"],
            )
        )


def _seed_tickets(db) -> None:
    if db.query(Ticket).first() is not None:
        return
    for t in _SEED_TICKETS:
        db.add(
            Ticket(
                id=allocate_ticket_id(db),
                dedup_key=_dedup_key(t["user_id"], t["description"]),
                user_id=t["user_id"],
                user_name=t["user_name"],
                description=t["description"],
                tried_steps=t["tried_steps"],
                evidence=t["evidence"],
                status="open",
                created_at=time.time(),
                replies=[],
            )
        )


def _dedup_key(user_id: str, description: str) -> str:
    return hashlib.sha256(f"{user_id}::{description}".encode()).hexdigest()


def seed_all() -> None:
    """预置全部演示数据（幂等：各表已有数据则跳过对应项）。"""
    db = SessionLocal()
    try:
        _seed_users(db)
        _seed_products(db)
        _seed_orders(db)
        _seed_knowledge(db)
        _seed_tickets(db)
        db.commit()
    finally:
        db.close()
