# AI 客服与工单助手

面向虚拟软件公司售后支持的最小 demo，用于演示「专业版导出功能失败 → 排查建议 → 创建工单」的完整链路。

## 当前状态（第 0 天）

- 后端：FastAPI，`/api/chat` 返回硬编码回复
- 前端：单页 HTML，模拟四步排查流程 + 创建工单
- 尚未接入：数据库、真实模型、知识检索（后续每日优化）

## 运行

```bash
# 后端（默认 127.0.0.1:8000）
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

浏览器打开 http://127.0.0.1:8000/ ，输入示例问题「专业版导出功能一直失败」查看效果。

## 目录

```
backend/
  main.py        # 应用入口 + 静态托管
  chat.py        # 聊天接口（硬编码）
  requirements.txt
frontend/
  index.html     # 单页 demo
```
