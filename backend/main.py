from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from backend.api.auth import auth_router
from backend.api.agent_internal import agent_router
from backend.api.chat import chat_router
from backend.api.knowledge import knowledge_router
from backend.api.tickets import tickets_router
from backend.database import init_db
from backend.seed import seed_all
from backend.schemas import HealthResponse

app = FastAPI(title="AI 客服与工单助手", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> PlainTextResponse:
    """未处理异常统一返回 500，并显式告知客户端不要复用这条连接。

    Starlette 写出 500 之后仍会把异常抛给 ASGI 服务器，服务器随即关闭当前
    的 keep-alive 连接；响应里若没有 ``Connection: close``，客户端会继续复用
    一条已经关闭的连接，下一个请求直接收到 RST（Connection reset by peer）。
    带上该头部后，客户端会主动换新连接，而不是把 500 之后的所有请求都撞死。
    """
    return PlainTextResponse(
        "Internal Server Error", status_code=500, headers={"Connection": "close"},
    )


app.include_router(chat_router, prefix="/api")
app.include_router(agent_router)
app.include_router(auth_router, prefix="/api")
app.include_router(tickets_router, prefix="/api")
app.include_router(knowledge_router, prefix="/api")

# 初始化数据库并预置演示数据（进程启动时执行一次）
init_db()
seed_all()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.get("/health", response_model=HealthResponse, summary="检查服务与模型配置")
def health() -> JSONResponse:
    """健康检查：同时报告模型能力配置状态。

    模型配置缺失不影响服务启动（检索与对话都有降级），但在这里提前
    暴露出来，省去「请求失败后逐层排查」的时间。
    """
    from backend.providers import capability_report

    return JSONResponse({"status": "ok", "stream": True, "models": capability_report()})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "admin.html")


@app.get("/settings")
def settings_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "settings.html")


@app.get("/knowledge")
def knowledge_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "knowledge.html")
