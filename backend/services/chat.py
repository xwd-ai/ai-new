import time
import math
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import text

from backend.database import SessionLocal
from backend.services import agent, citations, context, escalation, knowledge, llm, orders, tools
from backend.models import Conversation, Message, TriedStep
from backend.providers import ChatProvider

# 对话链路：会话与消息的存取、已尝试步骤的记录，以及「查订单 + 检索手册 + 生成回答」的编排。
# 路由定义在 backend.api.chat，此处仅承载存储与业务编排逻辑。

# 未注入时由 llm 在首次调用时装配，缺少配置不阻止应用导入。
_chat_provider: ChatProvider | None = None


def current_user(authorization: str):
    from backend.services import auth

    return auth.resolve_authorization(authorization)


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


def append_turn(conversation_id: str, message: str, reply: str) -> None:
    """同事务保存一轮消息与更新时间，异常时整轮回滚。"""
    user_at = time.time()
    assistant_at = max(time.time(), math.nextafter(user_at, math.inf))
    with SessionLocal() as db, db.begin():
        db.add_all([
            Message(
                id=uuid.uuid4().hex, conversation_id=conversation_id,
                role=role, content=content, at=at,
            )
            for role, content, at in (
                ("user", message, user_at), ("assistant", reply, assistant_at),
            )
        ])
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        if conv:
            conv.updated_at = assistant_at


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
        query = (
            db.query(Conversation)
            .filter(Conversation.user_id == user_id)
            .order_by(
                Conversation.updated_at.desc(), Conversation.created_at.desc(), Conversation.id.desc(),
            )
        )
        conv = query.first()
        if conv:
            return conv.id
        # 空结果不能作为创建依据：释放读事务后取得 SQLite 写锁，再查一次。
        # 锁仅覆盖首次创建，不覆盖模型回答或后续消息写入。
        db.rollback()
        db.execute(text("BEGIN IMMEDIATE"))
        conv = query.first()
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
            .order_by(Message.at.desc(), Message.id.desc())
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
        found = knowledge.search_for_chat(query)
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
    # 失败或尚未完成的调用不应继续暴露上一次的摘要。
    _last_execution.set(None)
    conversation_id = get_or_create_conversation(user_id)
    reply, sources, suggest, step, execution = generate_reply(
        user_id, message, history(conversation_id)
    )
    append_turn(conversation_id, message, reply)
    if step:
        append_tried_step(user_id, step)
    _last_execution.set(execution)
    return reply, sources, suggest


def generate_reply(user_id: str, message: str, prior_messages: list[dict]):
    """运行 Agent，返回回复及本轮元数据，由调用方保存消息。"""
    # 历史里的引用标记会被剥离：上轮引用过的来源不构成本轮回答的证据
    # 再按预算压缩，保留最近若干轮完整对话，避免长历史稀释当前问题
    compacted = context.compact_history(
        citations.history_as_context(prior_messages)
    )
    context_messages = compacted.messages

    # 匿名用户无订单，直接跳过「查订单」环节
    order = orders.query_order(user_id) if user_id != "anonymous" else None

    # 为本次检索到的资料分配请求内有效的短句柄，模型只看到编号
    registry = citations.SourceRegistry()

    # 第一轮检索用用户原话，保证「什么都不说也能答」的基本盘
    registry_tools = _build_tools(user_id, registry)
    tool_result = registry_tools.execute("search_knowledge", query=message)
    first_items = tool_result.data.get("items", []) if tool_result.success else []
    prompt_items = citations.build_prompt_items(first_items, registry)

    state = agent.AgentState(query=message)
    if first_items:
        state.remember(agent._format_items(prompt_items))
    if not tool_result.success:
        detail = f"知识检索失败：{tool_result.error}"
        state.remember(detail)
        state.emit(
            agent.EVENT_ERROR, message=detail,
            error_code=tool_result.error_code, retryable=tool_result.retryable,
        )

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
        found = knowledge.search_for_chat(query)
        return citations.build_prompt_items(found, registry)

    transition = agent.run_loop(state, step_fn=_step, search_fn=_search)
    summary = agent.summarize(state, transition)

    if transition.kind is agent.TransitionKind.COMPLETED:
        reply = transition.content
    elif transition.kind is agent.TransitionKind.MODEL_ERROR:
        reply = transition.detail
    else:
        reply = agent.finalize_answer(state, agent._STALLED_ANSWER)

    # 把模型输出里的编号还原成可读的来源标注；
    # 编造或未注册的编号会被丢弃，避免出现无出处的引用
    reply = citations.decode_refs(reply, registry)

    step = f"按《{first_items[0]['title']}》排查" if first_items else None

    # 转人工判定：按服务范围与排查情况给结论，并留下判定原因
    prior_unresolved = any(
        k in m.get("content", "")
        for m in context_messages
        if m.get("role") == "user"
        for k in ("还是不行", "还是没用", "没解决", "解决不了")
    )
    decision = escalation.decide_escalation(
        message,
        order=order,
        repeated_unresolved=prior_unresolved,
    )
    suggest = decision.suggest
    sources = citations.public_sources(registry)
    return reply, sources, suggest, step, (summary, decision)


def build_stream_messages(
    user_id: str, message: str
) -> tuple[list[dict], list[dict], str, bool]:
    """为流式回答准备消息、证据、对话 id 与转人工建议。

    与 build_reply 的差异：**不做 Agent 多轮决策**。边决策边流式无法保证
    输出一致 —— 决策可能要求再检索一轮，而流式内容已经发出去了。
    因此这里一次性完成检索，再让模型基于证据边生成边返回。

    返回 (messages, sources, conversation_id, suggest_ticket)。
    """
    conversation_id = get_or_create_conversation(user_id)
    messages, sources, suggest = prepare_stream(user_id, message, history(conversation_id))
    return messages, sources, conversation_id, suggest


def prepare_stream(user_id: str, message: str, prior_messages: list[dict]):
    """准备流式模型输入，由调用方提供历史并保存完整回复。"""
    compacted = context.compact_history(
        citations.history_as_context(prior_messages)
    )

    order = orders.query_order(user_id) if user_id != "anonymous" else None

    # 检索结果分配请求内有效的句柄，模型只看到编号，前端按编号回查原文
    registry = citations.SourceRegistry()
    found = knowledge.search_for_chat(message)
    prompt_items = citations.build_prompt_items(found, registry)

    messages = llm.build_messages(
        message,
        history=compacted.messages,
        order=order,
        knowledge=prompt_items,
        allow_action=False,
    )

    # 转人工判定与非流式路径保持同一口径
    prior_unresolved = any(
        k in m.get("content", "")
        for m in compacted.messages
        if m.get("role") == "user"
        for k in ("还是不行", "还是没用", "没解决", "解决不了")
    )
    decision = escalation.decide_escalation(
        message, order=order, repeated_unresolved=prior_unresolved
    )

    return (
        messages,
        citations.public_sources(registry),
        decision.suggest,
    )


# 当前执行上下文最近一次完整对话的摘要与判定，作为一组发布。
# 单独暴露而不是塞进三元组返回值，是为了不破坏既有调用方的解包写法；
# 需要执行过程的调用方（如接口层）在这里取。
_last_execution: ContextVar[tuple[agent.ExecutionSummary, escalation.EscalationDecision] | None] = ContextVar(
    'chat_last_execution', default=None,
)


@contextmanager
def execution_scope():
    """为一次 HTTP 请求建立空作用域，退出或异常时恢复调用方上下文。"""
    token = _last_execution.set(None)
    try:
        yield
    finally:
        _last_execution.reset(token)


def last_summary() -> agent.ExecutionSummary | None:
    """返回当前执行上下文最近一次完整对话的摘要。"""
    result = _last_execution.get()
    return result[0] if result else None


def last_escalation() -> escalation.EscalationDecision | None:
    """返回当前执行上下文最近一次完整对话的转人工判定。"""
    result = _last_execution.get()
    return result[1] if result else None
