import time
import uuid

from backend.database import SessionLocal
from backend.services import agent, citations, context, knowledge, llm, orders, tools
from backend.models import Conversation, Message, TriedStep
from backend.providers import build_chat_provider

# 对话链路：会话与消息的存取、已尝试步骤的记录，以及「查订单 + 检索手册 + 生成回答」的编排。
# 路由定义在 backend.api.chat，此处仅承载存储与业务编排逻辑。

# 对话链路的 Chat 能力，由适配层装配；可在测试时替换为假实现
_chat_provider = build_chat_provider()


def current_user(authorization: str):
    from backend.services import auth

    token = authorization.replace("Bearer ", "").strip()
    session = auth.get_session(token)
    if not session:
        return None
    return auth.find_by_username(session["username"])


def append_message(conversation_id: str, role: str, content: str) -> None:
    """向数据库追加一条消息，并刷新会话更新时间。"""
    db = SessionLocal()
    try:
        db.add(
            Message(
                id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                role=role,
                content=content,
                at=time.time(),
            )
        )
        conv = (
            db.query(Conversation)
            .filter(Conversation.id == conversation_id)
            .first()
        )
        if conv:
            conv.updated_at = time.time()
        db.commit()
    finally:
        db.close()


def append_tried_step(user_id: str, step: str) -> None:
    """记录一次「已尝试步骤」，供后续建单沉淀。"""
    db = SessionLocal()
    try:
        db.add(
            TriedStep(
                id=uuid.uuid4().hex,
                user_id=user_id,
                step=step,
                at=time.time(),
            )
        )
        db.commit()
    finally:
        db.close()


def get_tried_steps(user_id: str) -> str:
    db = SessionLocal()
    try:
        steps = (
            db.query(TriedStep)
            .filter(TriedStep.user_id == user_id)
            .order_by(TriedStep.at.asc())
            .all()
        )
        return " · ".join(s.step for s in steps) if steps else ""
    finally:
        db.close()


def get_or_create_conversation(user_id: str) -> str:
    """取该用户最近一次会话；没有则新建一个。"""
    db = SessionLocal()
    try:
        conv = (
            db.query(Conversation)
            .filter(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .first()
        )
        if conv:
            return conv.id
        now = time.time()
        conv = Conversation(
            id=uuid.uuid4().hex,
            user_id=user_id,
            title="",
            created_at=now,
            updated_at=now,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return conv.id
    finally:
        db.close()


def history(conversation_id: str, limit: int = 8) -> list[dict]:
    """拉取最近 N 条消息作为上下文。"""
    db = SessionLocal()
    try:
        messages = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.at.desc())
            .limit(limit)
            .all()
        )
        # 倒序取回后反转为时间正序
        return [
            {"role": m.role, "content": m.content}
            for m in reversed(messages)
        ]
    finally:
        db.close()


def _build_tools(user_id: str, registry: citations.SourceRegistry) -> tools.ToolRegistry:
    """为本轮对话装配可用工具。

    工具只做「取数」，不负责组织回答：检索结果仍由调用方登记句柄、
    拼进上下文，这样引用身份的分配权始终留在对话层手里。
    """

    def search_knowledge_tool(query: str = "", **_: object) -> tools.ToolResult:
        found = knowledge.search_knowledge(query)
        return tools.ToolResult(success=True, data={"items": found})

    def query_order_tool(**_: object) -> tools.ToolResult:
        if user_id == "anonymous":
            return tools.ToolResult(success=True, data={"order": None})
        order_info = orders.query_order(user_id)
        return tools.ToolResult(success=True, data={"order": order_info})

    tool_registry = tools.ToolRegistry()
    tool_registry.register(
        "search_knowledge", "按关键词检索知识库资料", search_knowledge_tool
    )
    tool_registry.register("query_order", "查询当前用户订单与服务期", query_order_tool)
    return tool_registry


def build_reply(user_id: str, message: str) -> tuple[str, list[dict], bool]:
    """编排一次对话回复：先查订单，再让模型在循环里决定检索与作答。

    返回 (reply, sources, suggest_ticket)。sources 里带有的 ``handle``
    就是回答中引用用的编号，前端可据此回查原文。
    """
    conversation_id = get_or_create_conversation(user_id)
    # 历史里的引用标记会被剥离：上轮引用过的来源不构成本轮回答的证据
    # 再按预算压缩，保留最近若干轮完整对话，避免长历史稀释当前问题
    compacted = context.compact_history(
        citations.history_as_context(history(conversation_id))
    )
    context_messages = compacted.messages

    # 匿名用户无订单，直接跳过「查订单」环节
    order = orders.query_order(user_id) if user_id != "anonymous" else None

    # 为本次检索到的资料分配请求内有效的短句柄，模型只看到编号
    registry = citations.SourceRegistry()

    # 第一轮检索用用户原话，保证「什么都不说也能答」的基本盘
    first_items = knowledge.search_knowledge(message)
    prompt_items = citations.build_prompt_items(first_items, registry)

    state = agent.AgentState(query=message)
    if first_items:
        state.remember(agent._format_items(prompt_items))

    def _step(current: agent.AgentState) -> str:
        return llm.chat_step(
            message,
            history=context_messages,
            observations=current.observations,
            order=order,
            knowledge=prompt_items,
            provider=_chat_provider,
        )

    def _search(query: str) -> list[dict]:
        found = knowledge.search_knowledge(query)
        return citations.build_prompt_items(found, registry)

    # 检索经工具系统执行：工具内部自带异常收敛，失败会变成一条观察结果
    registry_tools = _build_tools(user_id, registry)
    tool_result = registry_tools.execute("search_knowledge", query=message)
    if not tool_result.success:
        state.remember(f"检索失败：{tool_result.error}")

    transition = agent.run_loop(state, step_fn=_step, search_fn=_search)
    summary = agent.summarize(state, transition)

    if transition.kind is agent.TransitionKind.COMPLETED:
        reply = transition.content
    elif transition.kind is agent.TransitionKind.MODEL_ERROR:
        reply = "抱歉，我这边暂时无法完成回答，请稍后再试。"
    else:
        reply = agent.finalize_answer(state, agent._STALLED_ANSWER)

    # 把模型输出里的编号还原成可读的来源标注；
    # 编造或未注册的编号会被丢弃，避免出现无出处的引用
    reply = citations.decode_refs(reply, registry)

    append_message(conversation_id, "user", message)
    append_message(conversation_id, "assistant", reply)

    if first_items:
        append_tried_step(user_id, f"按《{first_items[0]['title']}》排查")

    suggest = any(
        k in message
        for k in ("失败", "报错", "不行", "解决不了", "无效", "工单", "不能用", "退款")
    )
    sources = citations.public_sources(registry)
    global _last_summary
    _last_summary = summary
    return reply, sources, suggest


# 最近一次对话的执行摘要。
# 单独暴露而不是塞进三元组返回值，是为了不破坏既有调用方的解包写法；
# 需要执行过程的调用方（如接口层）在这里取。
_last_summary: agent.ExecutionSummary | None = None


def last_summary() -> agent.ExecutionSummary | None:
    """返回最近一次对话的执行摘要（含事件列表）。"""
    return _last_summary
