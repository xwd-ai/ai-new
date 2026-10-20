# 数据模型层：按领域拆分到各子模块，此处统一 re-export 对外暴露。
# 各模型按阶段迁移，逐步从 JSON 文件存储切到数据库。

from backend.models.conversation import Conversation, Message, TriedStep
from backend.models.knowledge import KnowledgeDoc
from backend.models.order import Order, Product
from backend.models.ticket import Ticket
from backend.models.user import Session, User

__all__ = [
    "Conversation",
    "KnowledgeDoc",
    "Message",
    "Order",
    "Product",
    "Session",
    "Ticket",
    "TriedStep",
    "User",
]
