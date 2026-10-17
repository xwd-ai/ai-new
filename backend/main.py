from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.auth_api import auth_router
from backend.chat import chat_router
from backend.tickets_api import tickets_router
from backend import auth, knowledge, orders, tickets
from backend.database import init_db

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

# 初始化数据库并预置演示数据（进程启动时执行一次）
init_db()
auth.seed_users()
orders.seed_orders()
tickets.seed_tickets()
knowledge.seed_knowledge()

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/assets", StaticFiles(directory=FRONTEND_DIR), name="assets")


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "login.html")


@app.get("/admin")
def admin_page() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "admin.html")
