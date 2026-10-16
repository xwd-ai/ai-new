import json
import os
import threading
from pathlib import Path

# 极简 JSON 文件持久化（demo 阶段不引数据库）
# 所有业务状态落到 data.json，进程重启不丢

_DATA_DIR = Path(__file__).resolve().parent
_DATA_FILE = _DATA_DIR / "data.json"

_lock = threading.Lock()

# 默认结构
_DEFAULT = {
    "tickets": {},       # ticket_id -> ticket
    "ticket_seq": 1000,  # 工单号自增
    "sessions": {},      # token -> session
    "conversations": {}, # user_id -> [ {role, content, at} ]
}


def _load() -> dict:
    if not _DATA_FILE.exists():
        return json.loads(json.dumps(_DEFAULT))
    try:
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 补齐可能缺失的键
        for k, v in _DEFAULT.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError):
        return json.loads(json.dumps(_DEFAULT))


def _save(data: dict) -> None:
    with open(_DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read() -> dict:
    with _lock:
        return _load()


def write(data: dict) -> None:
    with _lock:
        _save(data)
