"""上下文压缩。

对话变长后，不能把所有历史都塞给模型：既有 token 成本，也会稀释当前
问题。常见做法是「只保留最近若干条」，但直接按条数截断会踩两个坑：

1. **截断点落在一个不完整的对话回合中间**：上下文以助手的话开头，
   模型会误以为那是本轮正在进行的对话。
2. **丢掉用户提问却留下对应的回答**：模型看到一句没有来源的结论，
   容易顺着它编下去。

因此这里不按「条数」而是按「保留预算」来选窗口，并保证窗口开头是
一条完整的用户消息——被截掉的部分宁可多扔一条，也不留下半截回合。
"""

from __future__ import annotations

from dataclasses import dataclass

# 保留窗口的最小消息数：再短就失去上下文意义了
MIN_KEEP_MESSAGES = 4

# 单条消息参与预算计算时的默认字符上限，避免一条超长消息吞掉整个窗口
MAX_MESSAGE_CHARS = 2000


@dataclass
class CompactionResult:
    """一次压缩的结果。"""

    messages: list[dict]
    dropped: int
    truncated: int

    @property
    def changed(self) -> bool:
        return self.dropped > 0 or self.truncated > 0


def _trim_content(content: str, limit: int = MAX_MESSAGE_CHARS) -> tuple[str, bool]:
    """截断单条超长消息，保留开头部分。"""
    if len(content) <= limit:
        return content, False
    head = content[:limit]
    return head + f"\n…（已省略 {len(content) - limit} 字）", True


def _starts_with_user(messages: list[dict], start: int) -> bool:
    """窗口是否从一条用户消息开始。"""
    return 0 <= start < len(messages) and messages[start].get("role") == "user"


def _align_to_user_turn(messages: list[dict], start: int) -> int:
    """把窗口起点调整到一个完整的用户回合开头。

    如果起点落在助手消息上，说明上一个用户问题被截掉了——把它也丢掉，
    避免留下「没有提问的回答」。一直前移到用户消息或数组开头。
    """
    index = start
    while 0 < index < len(messages) and messages[index].get("role") != "user":
        index += 1
    # 走到末尾说明后面全是助手消息，全丢；否则让窗口从这条用户消息开始
    return index


def compact_history(
    messages: list[dict],
    *,
    max_messages: int = 8,
    max_chars: int = 6000,
) -> CompactionResult:
    """压缩历史消息，返回保留窗口。

    保留策略：
    - 从最新往前累计，直到条数或字符预算用尽；
    - 窗口起点对齐到完整的用户回合，不截出半截对话；
    - 单条超长消息按其上限截断，但至少保留它的开头。

    被压缩掉的内容不做摘要——这个项目的历史主要用于维持对话连贯，
    与其用模型再花一次调用去概括旧话题，不如把预算留给当前问题。
    """
    if not messages:
        return CompactionResult(messages=[], dropped=0, truncated=0)

    # 先截断单条超长消息
    normalized: list[dict] = []
    truncated_count = 0
    for message in messages:
        content, did_truncate = _trim_content(str(message.get("content", "")))
        if did_truncate:
            truncated_count += 1
        normalized.append({"role": message.get("role", ""), "content": content})

    total_chars = 0
    keep_from = len(normalized)
    for index in range(len(normalized) - 1, -1, -1):
        remaining = len(normalized) - keep_from
        next_chars = total_chars + len(normalized[index]["content"])

        # 条数已够且字符预算将超，停止外扩
        if remaining >= max_messages or next_chars > max_chars:
            if remaining >= MIN_KEEP_MESSAGES:
                break

        keep_from = index
        total_chars = next_chars

        if remaining + 1 >= max_messages and total_chars >= max_chars:
            break

    # 对齐到完整回合：不留「没有提问的回答」
    aligned_from = _align_to_user_turn(normalized, keep_from)
    if not _starts_with_user(normalized, aligned_from):
        aligned_from = len(normalized)

    kept = normalized[aligned_from:]
    return CompactionResult(
        messages=kept,
        dropped=len(normalized) - len(kept),
        truncated=truncated_count,
    )
