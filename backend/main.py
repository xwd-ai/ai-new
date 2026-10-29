from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.auth import auth_router
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

app.include_router(chat_router, prefix="/api")
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

    return JSONResponse({"status": "ok", "models": capability_report()})


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
