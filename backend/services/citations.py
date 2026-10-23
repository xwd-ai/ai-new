"""引用句柄协议。

模型不应该直接看到数据库主键或内部标识：它们又长又容易抄错，而且会把
内部结构泄露到回答里。这里给每个来源分配一个**请求内有效**的短句柄
（``c1``、``c2``……），模型只引用句柄，系统在输出时再解码回可点击的来源。

关键约定：
- 句柄只在一次请求/一次回答内有效，绝不跨请求持久化或复用。
- 模型回显的句柄必须确实由本次检索注册过；未注册的句柄一律丢弃，
  避免模型编造来源。
- 历史回答里的引用只用于导航，不能作为当前回答的证据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 句柄形如 c1、c2……只允许 ASCII 字母 + 数字
_HANDLE_RE = re.compile(r"^c[1-9][0-9]*$")

# 模型输出里的引用标记：<ref id="c1"/>，容忍单双引号与多余空格
_REF_TAG_RE = re.compile(r"<ref\s+id\s*=\s*[\"']([^\"']+)[\"']\s*/?>", re.IGNORECASE)

# 兜底：模型可能输出裸句柄 <ref c1> 这类不规范写法
_REF_LOOSE_RE = re.compile(r"<ref\s+([^>]*?)\s*/?>", re.IGNORECASE)


@dataclass
class Citation:
    """一个已注册的来源句柄。"""

    handle: str
    id: str
    title: str
    kind: str
    content_preview: str = ""
    # 是否由本次检索结果直接提供（可作为回答证据）
    evidence: bool = True


@dataclass
class SourceRegistry:
    """一次回答内有效的句柄注册表。

    句柄按注册顺序递增分配，永不回收、永不复用：同一份来源在一次
    回答里始终对应同一个句柄，模型多次引用同一来源不会产生歧义。
    """

    _by_handle: dict[str, Citation] = field(default_factory=dict)
    _handle_by_id: dict[str, str] = field(default_factory=dict)
    _next: int = 1

    def register(self, item: dict, *, evidence: bool = True) -> str:
        """为一条来源注册句柄，返回句柄名。

        同一条来源重复注册会返回同一句柄，不会占用新的编号。
        """
        source_id = str(item.get("id", "")).strip()
        if not source_id:
            return ""

        existing = self._handle_by_id.get(source_id)
        if existing:
            # 重复注册：若本次可作证据而首次不可，则升级为证据
            if evidence:
                self._by_handle[existing].evidence = True
            return existing

        handle = f"c{self._next}"
        self._next += 1
        content = str(item.get("content", ""))
        self._by_handle[handle] = Citation(
            handle=handle,
            id=source_id,
            title=str(item.get("title", "")),
            kind=str(item.get("kind", "")),
            content_preview=content[:80],
            evidence=evidence,
        )
        self._handle_by_id[source_id] = handle
        return handle

    def register_many(self, items: list[dict], *, evidence: bool = True) -> list[str]:
        return [self.register(i, evidence=evidence) for i in items]

    def resolve(self, handle: str) -> Citation | None:
        """把句柄还原为来源；未知句柄返回 None。"""
        return self._by_handle.get(str(handle).strip().lower())

    def is_known(self, handle: str) -> bool:
        return handle.strip().lower() in self._by_handle

    def __len__(self) -> int:
        return len(self._by_handle)

    def citations(self) -> list[Citation]:
        """按注册顺序返回全部来源。"""
        return sorted(self._by_handle.values(), key=lambda c: int(c.handle[1:]))


def build_prompt_items(items: list[dict], registry: SourceRegistry) -> list[dict]:
    """把检索结果整理成给模型看的参考资料。

    模型的输入里只出现 ``handle``，不出现数据库主键与内部字段，
    这样模型的引用天然只能落在本次提供的来源上。
    """
    prompt_items: list[dict] = []
    for item in items:
        handle = registry.register(item, evidence=True)
        if not handle:
            continue
        prompt_items.append(
            {
                "handle": handle,
                "title": item.get("title", ""),
                "content": item.get("content", ""),
                "kind": item.get("kind", ""),
            }
        )
    return prompt_items


def decode_refs(text: str, registry: SourceRegistry) -> str:
    """把模型输出中的 ``<ref id="cN"/>`` 解码为可读的来源标注。

    - 未注册的句柄（模型编造或串了历史轮次的句柄）直接丢弃，
      保证回答里不会出现无出处的引用。
    - 非证据来源（仅导航用）同样不可引用。
    """
    if not text:
        return text

    def _replace(match: re.Match[str]) -> str:
        handle = match.group(1).strip().lower()
        citation = registry.resolve(handle)
        if citation is None or not citation.evidence:
            return ""
        return f"[来源: {citation.title}]"

    decoded = _REF_TAG_RE.sub(_replace, text)

    # 处理不合规范的 <ref c1> 写法
    def _replace_loose(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            return ""
        candidate = inner
        # 可能是 "id=c1" 或直接 "c1"
        if "=" in candidate:
            candidate = candidate.split("=", 1)[1]
        candidate = candidate.strip().strip("\"'").lower()
        citation = registry.resolve(candidate)
        if citation is None or not citation.evidence:
            return ""
        return f"[来源: {citation.title}]"

    return _REF_LOOSE_RE.sub(_replace_loose, decoded)


def extract_handles(text: str) -> list[str]:
    """从模型输出里抽出它引用过的句柄（含未注册的），用于诊断。"""
    if not text:
        return []
    found = [m.group(1).strip().lower() for m in _REF_TAG_RE.finditer(text)]
    found.extend(m.group(1).strip() for m in _REF_LOOSE_RE.finditer(text))
    seen: list[str] = []
    for h in found:
        if _HANDLE_RE.match(h) and h not in seen:
            seen.append(h)
    return seen


def public_sources(registry: SourceRegistry) -> list[dict]:
    """给前端的来源列表：句柄 + 可回查的最小信息。"""
    return [
        {
            "id": c.id,
            "handle": c.handle,
            "title": c.title,
            "kind": c.kind,
        }
        for c in registry.citations()
    ]


# ---------------------------------------------------------------------------
# 历史引用处理
# ---------------------------------------------------------------------------

# 已解码的来源标注：[来源: xxx]
_SOURCE_LABEL_RE = re.compile(r"\[来源:\s*[^\]]*\]")


def strip_citation_marks(text: str) -> str:
    """剥离文本里的引用标记，只留下可读正文。

    用于历史消息：上一轮回答里引用了哪些来源，那只证明上一轮检索到了
    这些内容，**不能**作为本轮回答的证据。把标记从历史里去掉，
    模型就不会把旧引用误当成本轮的可用证据继续沿用。
    """
    if not text:
        return text
    cleaned = _REF_TAG_RE.sub("", text)
    cleaned = _REF_LOOSE_RE.sub("", cleaned)
    cleaned = _SOURCE_LABEL_RE.sub("", cleaned)
    # 清理引用被移除后残留的连续空格
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def history_as_context(messages: list[dict]) -> list[dict]:
    """把历史消息转成喂给模型的上下文。

    助手侧的引用标记会被剥离：历史引用只用于维持对话连贯性
    （知道上轮聊了什么），不构成对本轮回答的授权。
    用户侧消息原样保留。
    """
    context: list[dict] = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")
        if role == "assistant":
            content = strip_citation_marks(content)
        context.append({"role": role, "content": content})
    return context

