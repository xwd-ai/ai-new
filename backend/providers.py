"""OpenAI 兼容适配层。

将「调用大模型」这件事抽象成统一接口，上层业务只依赖抽象能力，
不直接接触具体的 SDK 与模型供应商。这样后续更换模型、接入
Embedding / Rerank 时无需改动业务链路。
"""

import http.client
import json
import os
import socket
import ssl
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import Iterator, Protocol, Sequence
from urllib.parse import urlsplit

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

    def chat_stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float = 0.7,
    ) -> Iterator[str]:
        """流式生成回复，逐段产出增量文本。

        每个 chunk 只带增量 delta，调用方拼起来才是完整回答。
        末尾的空 chunk（只带 finish_reason）直接跳过，避免产出空串。
        """
        stream = self._client.chat.completions.create(
            model=model or self._model,
            messages=list(messages),
            temperature=temperature,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is not None and delta.content:
                yield delta.content


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


class EmbeddingHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, *, context, timeout, socket_path):
        super().__init__(host, port, context=context, timeout=timeout)
        self._socket_path = socket_path
        self._tls = context

    def connect(self):
        if not self._socket_path:
            super().connect()
            return
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            sock.connect(self._socket_path)
            self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


class OpenAICompatEmbeddingProvider:
    """OpenAI 兼容的向量接口；E5 部署同时区分查询与文档。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        e5: bool = False,
        ca_file: str = "",
        socket_path: str = "",
    ) -> None:
        self._model = model or os.getenv("EMBEDDING_MODEL", "deepseek-embedding")
        self._e5 = e5
        self._passages: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_lock = Lock()
        if e5:
            self._endpoint = urlsplit(base_url or "")
            if self._endpoint.scheme != "https" or not self._endpoint.hostname:
                raise ValueError("Embedding endpoint must use HTTPS")
            self._key = api_key
            self._socket_path = socket_path
            self._tls = ssl.create_default_context(cafile=ca_file or None)
        else:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=api_key or os.getenv("DEEPSEEK_API_KEY", ""),
                base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            )

    def _request(self, texts: Sequence[str], model: str) -> list[list[float]]:
        if self._e5:
            entries = self._request_private(texts, model)
        else:
            response = self._client.embeddings.create(model=model, input=list(texts))
            entries = [{"index": entry.index, "embedding": entry.embedding} for entry in response.data]
        entries.sort(key=lambda entry: entry["index"])
        if [entry["index"] for entry in entries] != list(range(len(texts))):
            raise ValueError("incomplete embedding response")
        return [entry["embedding"] for entry in entries]

    def _request_private(self, texts: Sequence[str], model: str) -> list[dict]:
        body = json.dumps({"model": model, "input": list(texts), "encoding_format": "float"}, ensure_ascii=False).encode()
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        target = self._endpoint.path.rstrip("/") + "/embeddings"
        for attempt in range(2):
            connection = EmbeddingHTTPSConnection(
                self._endpoint.hostname, self._endpoint.port,
                context=self._tls, timeout=5, socket_path=self._socket_path,
            )
            try:
                connection.request("POST", target, body=body, headers=headers)
                response = connection.getresponse()
                if response.status == 200:
                    return json.loads(response.read(4 << 20))["data"]
                if response.status not in (429, 503) or attempt:
                    raise ConnectionError(f"Embedding HTTP {response.status}")
            finally:
                connection.close()
            time.sleep(0.3)

    def embed(self, text: str, *, model: str | None = None) -> list[float]:
        query = "query: " + text[:2000] if self._e5 else text
        return self._request([query], model or self._model)[0]

    def embed_many(self, texts: Sequence[str], *, model: str | None = None) -> list[list[float]]:
        if not texts:
            return []
        if not self._e5:
            return self._request(texts, model or self._model)
        passages = ["passage: " + text[:2000] for text in texts]
        selected_model = model or self._model
        if selected_model != self._model:
            return self._batch_passages(passages, selected_model)

        with self._cache_lock:
            cached = {}
            for passage in dict.fromkeys(passages):
                if passage in self._passages:
                    self._passages.move_to_end(passage)
                    cached[passage] = self._passages[passage]
        missing = list(dict.fromkeys(passage for passage in passages if passage not in cached))
        if missing:
            fetched = dict(zip(missing, self._batch_passages(missing, selected_model), strict=True))
            cached.update(fetched)
            with self._cache_lock:
                self._passages.update(fetched)
                while len(self._passages) > 2048:
                    self._passages.popitem(last=False)
        return [cached[passage] for passage in passages]

    def _batch_passages(self, passages: list[str], model: str) -> list[list[float]]:
        vectors = []
        for start in range(0, len(passages), 16):
            vectors.extend(self._request(passages[start:start + 16], model))
        return vectors


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


@lru_cache(maxsize=1)
def _embedding_client(key: str, url: str, model: str, ca_file: str, socket_path: str) -> EmbeddingProvider:
    return OpenAICompatEmbeddingProvider(
        api_key=key, base_url=url, model=model, e5=True, ca_file=ca_file, socket_path=socket_path,
    )


def build_embedding_provider() -> EmbeddingProvider | None:
    """仅在显式配置外部 Embedding 时构建客户端。"""
    key = os.getenv("EMBEDDING_API_KEY", "").strip()
    url = os.getenv("EMBEDDING_BASE_URL", "").strip()
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    if not _check_key("EMBEDDING_API_KEY")[0] or not url or not model:
        return None
    return _embedding_client(
        key, url, model, os.getenv("EMBEDDING_CA_FILE", ""), os.getenv("EMBEDDING_SOCKET", ""),
    )


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
    embedding_ok, embedding_hint = _check_key("EMBEDDING_API_KEY")
    if embedding_ok and not all((os.getenv(name) or "").strip() for name in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL")):
        embedding_ok, embedding_hint = False, "请配置 EMBEDDING_BASE_URL 和 EMBEDDING_MODEL"
    embedding_status = CapabilityStatus("embedding", embedding_ok, embedding_hint)
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
