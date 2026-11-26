"""仅为历史 Python HTTP 契约测试挂载参考路由。"""

from fastapi import FastAPI

from backend.tests.reference_api.auth import auth_router
from backend.tests.reference_api.chat import chat_router
from backend.tests.reference_api.knowledge import migrated_router
from backend.tests.reference_api.tickets import tickets_router


def mount_reference_routes(app: FastAPI) -> None:
    for router in (auth_router, chat_router, migrated_router, tickets_router):
        app.include_router(router, prefix="/api")
