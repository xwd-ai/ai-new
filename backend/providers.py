"""OpenAI 兼容适配层。

将「调用大模型」这件事抽象成统一接口，上层业务只依赖抽象能力，
不直接接触具体的 SDK 与模型供应商。这样后续更换模型、接入
Embedding / Rerank 时无需改动业务链路。
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, Sequence

from backend.errors import ModelConfigurationError


class Message(dict):
    """一条对话消息，等价于 OpenAI 的 messages 元素。

    约定键：role（system/user/assistant）、content（文本内容）。
    """


class ChatProvider(Protocol):
    """Chat 能力抽象：一次对话补全请求，返回模型文本回复。"""

    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> str:
        """根据消息序列生成回复。"""
        ...


class OpenAICompatChatProvider:
    """基于 OpenAI 兼容接口的 Chat 实现。

    通过 base_url / api_key 指向任意 OpenAI 兼容服务，默认对接
    DeepSeek，也可换成其它兼容供应商而无需改动调用方。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        )
        self._model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        from openai import OpenAI
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)

    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> str:
        resp = self._client.chat.completions.create(
            model=model or self._model,
            messages=list(messages),
            temperature=temperature,
        )
        return resp.choices[0].message.content or ""


class EmbeddingProvider(Protocol):
    """Embedding 能力抽象：把文本编码为向量，供检索做相似度计算。"""

    def embed(self, text: str, *, model: str | None = None) -> list[float]:
        """返回文本对应的向量。"""
        ...

    def embed_many(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        """批量编码，返回与输入等长的向量列表。"""
        ...


class OpenAICompatEmbeddingProvider:
    """基于 OpenAI 兼容接口的 Embedding 实现。

    通过 base_url / api_key 指向任意 OpenAI 兼容服务。默认对接
    DeepSeek 的向量接口，也可换成其它兼容供应商而无需改动调用方。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        )
        self._model = model or os.getenv(
            "EMBEDDING_MODEL", "deepseek-embedding"
        )
        from openai import OpenAI
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)

    def embed(self, text: str, *, model: str | None = None) -> list[float]:
        return self.embed_many([text], model=model)[0]

    def embed_many(
        self, texts: Sequence[str], *, model: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(
            model=model or self._model, input=list(texts)
        )
        return [d.embedding for d in resp.data]


class RerankResult:
    """一条重排结果：候选文档及其与查询的相关性分数。"""

    def __init__(self, index: int, relevance_score: float) -> None:
        self.index = index
        self.relevance_score = relevance_score

    def __repr__(self) -> str:
        return f"RerankResult(index={self.index}, relevance_score={self.relevance_score})"


class RerankProvider(Protocol):
    """Rerank 能力抽象：对候选文档按与查询的相关性重新排序。"""

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        model: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        """按相关性从高到低返回重排结果。"""
        ...


class OpenAICompatRerankProvider:
    """基于 OpenAI 兼容接口的 Rerank 实现。

    通过 base_url / api_key 指向任意 OpenAI 兼容服务，调用其重排
    接口，返回每个候选文档的相关性分数并按分数降序排列。
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._base_url = base_url or os.getenv(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        )
        self._model = model or os.getenv("RERANK_MODEL", "deepseek-reranker")
        from openai import OpenAI
        self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)

    def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        model: str | None = None,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        if not documents:
            return []
        resp = self._client.rerank.create(
            model=model or self._model,
            query=query,
            documents=list(documents),
            top_n=top_n if top_n is not None else len(documents),
        )
        results = [
            RerankResult(item.index, item.relevance_score) for item in resp.results
        ]
        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results


def build_chat_provider() -> ChatProvider:
    """按环境变量装配一个 Chat 能力实现。"""
    ok, hint = _check_key()
    if not ok:
        raise ModelConfigurationError(hint)
    return OpenAICompatChatProvider()


def build_embedding_provider() -> EmbeddingProvider:
    """按环境变量装配一个 Embedding 能力实现。"""
    return OpenAICompatEmbeddingProvider()


def build_rerank_provider() -> RerankProvider:
    """按环境变量装配一个 Rerank 能力实现。"""
    return OpenAICompatRerankProvider()


# ---------------------------------------------------------------------------
# 配置检查
# ---------------------------------------------------------------------------

# 占位密钥：本地联调时允许不填真实密钥，但要能识别出来并给出提示，
# 而不是等到真正调用模型才报一个看不懂的 SDK 错误。
_PLACEHOLDER_KEYS = {"", "sk-placeholder-not-real-key", "your-api-key"}


@dataclass
class CapabilityStatus:
    """一项模型能力的配置状态。"""

    capability: str
    available: bool
    # 配置齐全且不是占位值时为空；否则说明缺什么、怎么补
    hint: str = ""

    def to_dict(self) -> dict:
        return {
            "capability": self.capability,
            "available": self.available,
            "hint": self.hint,
        }


def _check_key(env_name: str = "DEEPSEEK_API_KEY") -> tuple[bool, str]:
    """检查密钥是否可用，返回 (是否可用, 提示)。"""
    value = (os.getenv(env_name) or "").strip()
    if not value:
        return False, f"未配置 {env_name}，请在 backend/.env 中填写后重启服务"
    if value.lower() in _PLACEHOLDER_KEYS:
        return False, f"{env_name} 仍是占位值，替换为真实密钥后重启服务"
    return True, ""


def inspect_capabilities() -> list[CapabilityStatus]:
    """检查各项模型能力的配置状态。

    把「能不能用」提前告诉调用方，比等到请求链路上抛异常再排查成本低得多。
    缺失任何一项都不影响应用启动——检索与对话都有降级路径。
    """
    ok, hint = _check_key()

    chat_status = CapabilityStatus("chat", ok, hint)
    embedding_status = CapabilityStatus("embedding", ok, hint or "")
    rerank_status = CapabilityStatus("rerank", ok, hint or "")

    if ok:
        # 配置齐全时再检查模型名是否落在合理形态，避免拼写错误一路带到调用
        chat_model = (os.getenv("DEEPSEEK_MODEL") or "deepseek-chat").strip()
        if chat_model and " " in chat_model:
            chat_status = CapabilityStatus(
                "chat", False, f"DEEPSEEK_MODEL 含空格，可能是误填：{chat_model!r}"
            )

    return [chat_status, embedding_status, rerank_status]


def capability_report() -> dict:
    """汇总报告，便于健康检查接口直接返回。"""
    statuses = inspect_capabilities()
    return {
        "all_available": all(s.available for s in statuses),
        "capabilities": [s.to_dict() for s in statuses],
    }
