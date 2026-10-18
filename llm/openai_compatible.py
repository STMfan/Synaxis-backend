"""Generic OpenAI Chat Completions adapter with normalized harness messages."""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx

from domain.errors import HarnessError
from llm.openai_stream import MAX_BYTES, collect_chat_stream, emit
from llm.providers import key_for, provider_for
from storage.budget import cny_cost
from storage.client_store import StoreError


def chat_messages(system, messages, provider):
    result = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            result.append({"role": role, "content": content})
            continue
        blocks, calls, reasoning = [], [], []
        pending_results = []
        for block in content:
            kind = block.get("type")
            if kind == "text":
                blocks.append({"type": "text", "text": block["text"]})
            elif kind == "image":
                if not provider.supports_images:
                    raise HarnessError("PROVIDER_IMAGES_UNSUPPORTED")
                source = block["source"]
                blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"},
                    }
                )
            elif kind == "thinking":
                reasoning.append(block.get("thinking", ""))
            elif kind == "tool_use" and role == "assistant":
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False),
                        },
                    }
                )
            elif kind == "tool_result" and role == "user":
                value = block.get("content", "")
                pending_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                    }
                )
            else:
                raise HarnessError("PROVIDER_UNSUPPORTED_HISTORY")
        # Tool replies must precede any later user message.
        result.extend(pending_results)
        if blocks or calls or (reasoning and role == "assistant"):
            row = {
                "role": role,
                "content": blocks
                if any(b["type"] != "text" for b in blocks)
                else "\n".join(b["text"] for b in blocks),
            }
            if calls:
                row["tool_calls"] = calls
            if reasoning and provider.send_reasoning_history:
                row["reasoning_content"] = "".join(reasoning)
            result.append(row)
    return result


def normalized_completion(payload):
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("Expected one choice")
    choice, content = choices[0], []
    message, reason = choice["message"], choice["finish_reason"]
    if reason not in ("stop", "length", "tool_calls") or message.get("refusal"):
        raise ValueError("Incomplete or refused completion")
    for field, kind in (("reasoning_content", "thinking"), ("content", "text")):
        value = message.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError("Invalid completion text")
        if value:
            content.append({"type": kind, kind: value})
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or bool(calls) != (reason == "tool_calls"):
        raise ValueError("Tool finish mismatch")
    seen = set()
    for call in calls:
        identifier, function = call["id"], call["function"]
        if (
            call.get("type") != "function"
            or not isinstance(identifier, str)
            or not identifier
            or identifier in seen
        ):
            raise ValueError("Invalid tool identifier")
        seen.add(identifier)
        name = function["name"]
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid tool name")
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        content.append({"type": "tool_use", "id": identifier, "name": name, "input": arguments})
    raw = payload.get("usage")
    usage = {}
    if raw is not None:
        prompt, output = raw["prompt_tokens"], raw["completion_tokens"]
        hit = (raw.get("prompt_tokens_details") or {}).get(
            "cached_tokens", raw.get("prompt_cache_hit_tokens", 0)
        )
        if any(type(n) is not int or n < 0 for n in (prompt, output, hit)) or hit > prompt:
            raise ValueError("Invalid usage")
        usage = {"input_tokens": prompt - hit, "cache_read_input_tokens": hit, "output_tokens": output}
    if not content:
        raise ValueError("Empty completion")
    return {
        "content": content,
        "usage": usage,
        "stop_reason": {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}[reason],
    }


class OpenAICompatibleClient:
    def __init__(self, settings, db, http, role):
        self.settings, self.db, self.http, self.role = settings, db, http, role

    async def complete(
        self,
        ctx,
        *,
        system,
        messages,
        model,
        effort=None,
        tools=None,
        on_text=None,
        on_thinking=None,
        max_tokens=None,
    ):
        name, provider = provider_for(self.settings, self.role)
        key = key_for(self.settings, self.role)
        if not key:
            raise HarnessError("PROVIDER_NOT_CONFIGURED")
        output, context = provider.output_limits.get(model), provider.context_limits.get(model)
        if output is None or context is None:
            raise HarnessError("PROVIDER_LIMITS_UNKNOWN")
        converted = chat_messages(system, messages, provider)
        has_images = any(isinstance(m["content"], list) for m in converted)
        # Use the entire input window for images; unknown visual tokenization
        # must never become an under-reservation.
        input_bound = (
            context if has_images else 2 * len(json.dumps(converted, ensure_ascii=False).encode()) + 8192
        )
        if input_bound > context:
            raise HarnessError("CONTEXT_TOO_LARGE")
        limit = min(
            max_tokens or self.settings.max_tokens, output, context if has_images else context - input_bound
        )
        body = {
            "model": model,
            "messages": converted,
            provider.max_tokens_field: limit,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not provider.stream_usage:
            body.pop("stream_options")
        if effort is not None and provider.reasoning_parameter != "none":
            if effort not in provider.reasoning_values:
                raise HarnessError("PROVIDER_EFFORT_UNSUPPORTED")
            body[provider.reasoning_parameter] = provider.reasoning_values[effort]
        elif effort == "max":
            raise HarnessError("PROVIDER_EFFORT_UNSUPPORTED")
        if tools:
            if not provider.supports_tools or any(t.get("type", "").startswith("web_search_") for t in tools):
                raise HarnessError("PROVIDER_TOOLS_UNSUPPORTED")
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t["input_schema"],
                    },
                }
                for t in tools
            ]
        if ctx.stage in ("planner", "formatter") and provider.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        if not has_images:
            input_bound = 2 * len(json.dumps(body, ensure_ascii=False).encode()) + 8192
            if input_bound > context:
                raise HarnessError("CONTEXT_TOO_LARGE")
            body[provider.max_tokens_field] = min(limit, context - input_bound)
            limit = body[provider.max_tokens_field]
        rates = provider.price_rates_cny.get(model)
        reservation = None
        if not provider.excluded_from_budget:
            if rates is None:
                raise HarnessError("BUDGET_PRICE_UNKNOWN")

            def reserve(tokens):
                return (
                    (
                        Decimal(input_bound) * Decimal(str(max(rates[:2])))
                        + Decimal(tokens) * Decimal(str(rates[2]))
                    )
                    / 1000000
                ).quantize(Decimal("0.000000001"))

            if ctx.session_id:
                available = Decimal(
                    self.db.budget_snapshot(ctx.session_id, ctx.user_id, self.settings)["available"]
                )
                low, high = 256, limit
                if limit < low or reserve(low) > available:
                    raise HarnessError("BUDGET_EXHAUSTED")
                while low < high:
                    middle = (low + high + 1) // 2
                    if reserve(middle) <= available:
                        low = middle
                    else:
                        high = middle - 1
                body[provider.max_tokens_field] = low
                reservation = reserve(low)
        if body[provider.max_tokens_field] < 256:
            raise HarnessError("CONTEXT_TOO_LARGE")
        try:
            call_id = self.db.begin_call(
                ctx.user_id,
                ctx.session_id,
                ctx.stage,
                model,
                body,
                currency="CNY",
                basis=f"excluded:{name}:configured"
                if provider.excluded_from_budget
                else f"configured:{name}:CNY-per-million",
                reservation=reservation,
                settings=self.settings,
            )
        except StoreError as exc:
            raise HarnessError(exc.code) from exc
        payload, normalized = None, None
        try:
            async with self.http.stream(
                "POST",
                provider.base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            ) as response:
                if response.status_code != 200:
                    raise HarnessError("PROVIDER_HTTP_ERROR", f"Provider HTTP {response.status_code}")
                if "text/event-stream" in response.headers.get("content-type", ""):
                    payload = await collect_chat_stream(response.aiter_lines(), on_text, on_thinking)
                else:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_BYTES:
                            raise ValueError("Oversized completion")
                    payload = json.loads(raw)
                    normalized = normalized_completion(payload)
                    for block in normalized["content"]:
                        if block["type"] == "text" and on_text:
                            await emit(on_text, block["text"])
                        elif block["type"] == "thinking" and on_thinking:
                            await emit(on_thinking, block["thinking"])
            normalized = normalized or normalized_completion(payload)
            cost = (
                Decimal(0)
                if provider.excluded_from_budget
                else cny_cost(
                    self.settings, model, normalized["usage"], snapshot=SimpleNamespace(rates=rates)
                )
                if normalized["usage"]
                else None
            )
            self.db.finish_call(call_id, response=payload, usage=normalized["usage"], cost=cost)
            return normalized
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            code = (
                "CANCELLED"
                if cancelled
                else exc.code
                if isinstance(exc, HarnessError)
                else (
                    "PROVIDER_TRANSPORT_ERROR"
                    if isinstance(exc, (OSError, httpx.TransportError))
                    else "PROVIDER_INVALID_RESPONSE"
                )
            )
            self.db.finish_call(
                call_id,
                response=payload,
                error=code,
                cost=Decimal(0) if provider.excluded_from_budget else None,
                status="cancelled" if cancelled else "failed",
            )
            if cancelled or isinstance(exc, HarnessError) or not isinstance(exc, Exception):
                raise
            raise HarnessError(code) from exc
