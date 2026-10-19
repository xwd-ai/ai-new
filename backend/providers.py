"""OpenAI 兼容适配层。

将「调用大模型」这件事抽象成统一接口，上层业务只依赖抽象能力，
不直接接触具体的 SDK 与模型供应商。这样后续更换模型、接入
Embedding / Rerank 时无需改动业务链路。
"""

import os
from abc import ABC, abstractmethod
from typing import Protocol, Sequence

from openai import OpenAI


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
    return OpenAICompatChatProvider()


def build_embedding_provider() -> EmbeddingProvider:
    """按环境变量装配一个 Embedding 能力实现。"""
    return OpenAICompatEmbeddingProvider()


def build_rerank_provider() -> RerankProvider:
    """按环境变量装配一个 Rerank 能力实现。"""
    return OpenAICompatRerankProvider()
