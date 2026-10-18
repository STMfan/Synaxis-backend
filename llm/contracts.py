from dataclasses import dataclass
from typing import Protocol

from llm.streaming import TextSink


@dataclass(frozen=True)
class CallContext:
    user_id: int
    session_id: str
    stage: str
    model_role: str | None = None


class LLM(Protocol):
    async def complete(
        self,
        ctx: CallContext,
        *,
        system: str,
        messages: list[dict],
        model: str,
        effort: str | None = None,
        tools: list[dict] | None = None,
        on_text: TextSink | None = None,
        on_thinking: TextSink | None = None,
        max_tokens: int | None = None,
    ) -> dict: ...
