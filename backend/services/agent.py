"""对话执行循环。

把「查订单 → 检索 → 生成」这条固定流水线，改造成一个可循环的执行过程：
每一步都可以读取已经观察到的信息，再决定下一步做什么，直到模型认为
信息足够、直接给出回答为止。

循环的三类动作：
- Think：把当前掌握的信息交给模型，让它决定是继续查资料还是直接回答。
- Act：执行模型要求的检索动作（查订单 / 查知识库）。
- Observe：把执行结果写回上下文，供下一轮 Think 使用。

「下一步做什么」在这里被显式建模成 ``LoopTransition``，而不是散落在
if/else 里——这样每一种退出原因（正常完成、达到轮次上限、模型异常）
都有自己的类型，调用方与日志都能准确区分「为什么停了」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# 单次对话允许的最大循环轮次。设置上界是为了避免模型反复要求检索、
# 迟迟不给结论，把一次请求拖成无限循环。
DEFAULT_MAX_ITERATIONS = 6


class TransitionKind(str, Enum):
    """循环的下一步动作。"""

    # 模型给出了最终回答，本轮对话结束
    COMPLETED = "completed"
    # 模型要求继续检索，进入下一轮
    CONTINUE = "continue"
    # 达到轮次上限，强制收口
    MAX_ITERATIONS = "max_iterations"
    # 模型调用失败
    MODEL_ERROR = "model_error"


@dataclass
class LoopTransition:
    """一轮循环结束后的状态转移。"""

    kind: TransitionKind
    # 模型这一轮的输出（原文）
    content: str = ""
    # 模型要求执行的动作，如 {"action": "search_knowledge", "query": "..."}
    action: dict | None = None
    # 说明信息，写入日志便于排查
    detail: str = ""

    @property
    def is_terminal(self) -> bool:
        """是否为终止状态（循环应当结束）。"""
        return self.kind in (
            TransitionKind.COMPLETED,
            TransitionKind.MAX_ITERATIONS,
            TransitionKind.MODEL_ERROR,
        )


@dataclass
class AgentState:
    """一次对话执行过程中的可变状态。

    这里只记录「循环需要知道的事」：走到第几轮、模型观察到了什么。
    业务数据（消息持久化、句柄分配）由调用方负责，不塞进循环状态里。
    """

    query: str
    # 已观察到的资料：订单信息、知识条目等，逐轮累积
    observations: list[str] = field(default_factory=list)
    # 已执行过的动作记录，避免同一轮重复请求同一件事
    executed: list[str] = field(default_factory=list)
    # 当前轮次（从 0 开始）
    iteration: int = 0
    # 循环结束时模型的最终回答
    answer: str = ""

    def remember(self, text: str) -> None:
        """记录一条观察结果，供下一轮模型参考。"""
        if text and text not in self.observations:
            self.observations.append(text)

    def action_key(self, action: dict) -> str:
        """把动作归一化成去重键，用于判断「这一步是否已经做过」。"""
        return f"{action.get('action', '')}:{action.get('query', '')}"


def finalize_answer(state: AgentState, fallback: str) -> str:
    """收口：确定本轮对话最终对外输出。

    达到轮次上限时模型可能还没来得及给出结论，此时用最后一条观察结果
    拼一个兜底回答，而不是返回空字符串。
    """
    if state.answer:
        return state.answer
    if state.observations:
        return fallback.format(evidence=state.observations[-1])
    return fallback.format(evidence="")


def should_continue(state: AgentState, max_iterations: int) -> bool:
    """是否还允许再走一轮。"""
    return state.iteration < max_iterations


# ---------------------------------------------------------------------------
# 执行循环
# ---------------------------------------------------------------------------

# 达到轮次上限时的兜底回答模板
_STALLED_ANSWER = (
    "我已经查到了这些可供参考的信息：\n{evidence}\n\n"
    "如果仍未能解决，可以继续补充现象，或让我帮你转人工处理。"
)


def run_loop(
    state: AgentState,
    *,
    step_fn,
    search_fn,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> LoopTransition:
    """执行对话循环，直到模型给结论、轮次用尽或模型调用失败。

    ``step_fn(state) -> str``      执行一轮推理，返回模型输出。
    ``search_fn(query) -> list``   执行一次检索，返回资料条目。

    每次进入循环先问模型，模型要么直接回答（COMPLETED），
    要么要求再查（CONTINUE）——查到的结果写回 state.observations，
    下一轮的模型就能看到它。这样「查几次、查什么」由模型自己决定，
    而不是由代码写死顺序。
    """
    while True:
        state.iteration += 1

        try:
            output = step_fn(state)
        except Exception as exc:  # noqa: BLE001 - 模型异常要转成可观测的终止态
            return LoopTransition(
                kind=TransitionKind.MODEL_ERROR,
                content="",
                detail=f"模型调用失败: {exc}",
            )

        if not output or not output.strip():
            # 空输出视为没有结论，交给下一轮；轮次用尽时由上层兜底
            if not should_continue(state, max_iterations):
                return LoopTransition(
                    kind=TransitionKind.MAX_ITERATIONS,
                    content="",
                    detail="模型多次返回空内容",
                )
            continue

        action = _parse_action(output)
        if action is None:
            # 没有检索指令，说明这是最终回答
            state.answer = output.strip()
            return LoopTransition(
                kind=TransitionKind.COMPLETED,
                content=state.answer,
            )

        # 模型要求继续检索
        key = state.action_key(action)
        if key in state.executed:
            # 同一个动作重复要求：说明模型在原地打转，直接收口
            return LoopTransition(
                kind=TransitionKind.MAX_ITERATIONS,
                content="",
                detail=f"重复请求同一检索动作: {key}",
            )
        state.executed.append(key)

        try:
            items = search_fn(action["query"])
        except Exception as exc:  # noqa: BLE001 - 检索失败不应中断整个对话
            state.remember(f"检索「{action['query']}」时出错：{exc}")
        else:
            if items:
                state.remember(_format_items(items))
            else:
                state.remember(f"检索「{action['query']}」没有找到相关资料。")

        if not should_continue(state, max_iterations):
            return LoopTransition(
                kind=TransitionKind.MAX_ITERATIONS,
                content="",
                action=action,
                detail="达到最大轮次",
            )


def _parse_action(text: str) -> dict | None:
    """解析模型输出中的检索指令。

    与 llm 层保持同一协议：以 ACTION: 开头的行表示「还想再查」。
    这里做一次轻量复刻，避免 agent 反向依赖 llm 造成循环导入。
    """
    if not text:
        return None
    stripped = text.strip()
    prefix = "ACTION:"
    if not stripped.upper().startswith(prefix):
        return None
    rest = stripped[len(prefix):].strip()
    if not rest:
        return None
    parts = rest.split(maxsplit=1)
    if parts[0].lower() != "search_knowledge":
        return None
    query = parts[1].strip() if len(parts) > 1 else ""
    return {"action": "search_knowledge", "query": query} if query else None


def _format_items(items: list[dict]) -> str:
    """把检索结果格式化成一条观察记录。"""
    blocks = []
    for item in items:
        handle = item.get("handle")
        label = f"[{handle}] " if handle else ""
        blocks.append(f"{label}{item.get('title', '')}\n{item.get('content', '')}")
    return "\n\n".join(blocks)
