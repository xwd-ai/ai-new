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


def build_chat_provider() -> ChatProvider:
    """按环境变量装配一个 Chat 能力实现。"""
    return OpenAICompatChatProvider()
