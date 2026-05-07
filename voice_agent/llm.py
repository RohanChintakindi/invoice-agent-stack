"""Thin LLM client abstraction.

Production code uses AnthropicClient. Tests inject FakeLLMClient with
scripted responses so the orchestrator can be exercised without API
calls or flakiness.
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Protocol


class LLMClient(Protocol):
    def chat(self, system: str, messages: list[dict], max_tokens: int = 500) -> str: ...
    def complete(self, system: str, user: str, max_tokens: int = 200) -> str: ...
    def astream_chat(
        self, system: str, messages: list[dict], max_tokens: int = 500
    ) -> AsyncIterator[str]: ...


class AnthropicClient:
    """Wraps the Anthropic SDK with the orchestrator's two call shapes."""

    def __init__(self, model: str | None = None, api_key: str | None = None):
        import anthropic

        self.model = model or os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=self._api_key)
        self._async_client = None  # lazy: only build when streaming is used

    def _get_async_client(self):
        if self._async_client is None:
            import anthropic

            self._async_client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._async_client

    def chat(self, system: str, messages: list[dict], max_tokens: int = 500) -> str:
        response = self.client.messages.create(
            model=self.model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        )
        # Concatenate text blocks (typically one).
        return "".join(block.text for block in response.content if hasattr(block, "text"))

    def complete(self, system: str, user: str, max_tokens: int = 200) -> str:
        return self.chat(system, [{"role": "user", "content": user}], max_tokens)

    async def astream_chat(
        self, system: str, messages: list[dict], max_tokens: int = 500
    ) -> AsyncIterator[str]:
        client = self._get_async_client()
        async with client.messages.stream(
            model=self.model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        ) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text


class FakeLLMClient:
    """Test double. Records every call. Returns scripted responses in order;
    falls back to a single default when the script runs out."""

    def __init__(
        self,
        chat_responses: list[str] | None = None,
        complete_responses: list[str] | None = None,
        default_chat: str = "Could we agree on a payment date this week?",
        default_complete: str = "verdict: pass\nwhy: looks fine\n",
    ):
        self.chat_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self._chat_responses = list(chat_responses or [])
        self._complete_responses = list(complete_responses or [])
        self.default_chat = default_chat
        self.default_complete = default_complete

    def chat(self, system: str, messages: list[dict], max_tokens: int = 500) -> str:
        self.chat_calls.append({"system": system, "messages": messages})
        if self._chat_responses:
            return self._chat_responses.pop(0)
        return self.default_chat

    def complete(self, system: str, user: str, max_tokens: int = 200) -> str:
        self.complete_calls.append({"system": system, "user": user})
        if self._complete_responses:
            return self._complete_responses.pop(0)
        return self.default_complete

    async def astream_chat(
        self, system: str, messages: list[dict], max_tokens: int = 500
    ) -> AsyncIterator[str]:
        # Same routing as chat() so tests can script streaming responses.
        self.chat_calls.append({"system": system, "messages": messages})
        text = self._chat_responses.pop(0) if self._chat_responses else self.default_chat
        # Yield word-sized fragments so iteration is observable in tests.
        words = text.split(" ")
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")
