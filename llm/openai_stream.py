"""Bounded Chat Completions SSE assembly, including tools and returned reasoning."""

import inspect
import json

MAX_BYTES = 8 * 1024**2


async def emit(sink, value):
    if sink and value:
        result = sink(value)
        if inspect.isawaitable(result):
            await result


async def collect_chat_stream(lines, on_text=None, on_thinking=None):
    data, text, thinking, tools = [], [], [], {}
    size = frame_size = 0
    done, finish, usage = False, None, None
    async for line in lines:
        if len(line.encode()) > MAX_BYTES:
            raise ValueError("Oversized SSE line")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            frame_size += len(line.encode())
            if frame_size > MAX_BYTES:
                raise ValueError("Oversized SSE frame")
        elif not line and data:
            raw, data, frame_size = "\n".join(data), [], 0
            if done:
                raise ValueError("Data after DONE")
            if raw == "[DONE]":
                done = True
                continue
            chunk = json.loads(raw)
            if not isinstance(chunk, dict) or "error" in chunk:
                raise ValueError("Invalid SSE chunk")
            choices = chunk.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError("Expected one choice")
            if chunk.get("usage") is not None:
                if not isinstance(chunk["usage"], dict):
                    raise ValueError("Invalid usage")
                usage = chunk["usage"]
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("Invalid choice index")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("refusal"):
                raise ValueError("Invalid or refused completion")
            for field, fragments, sink in (
                ("content", text, on_text),
                ("reasoning_content", thinking, on_thinking),
            ):
                value = delta.get(field)
                if value is not None:
                    if not isinstance(value, str) or (value and finish is not None):
                        raise ValueError("Invalid or late delta")
                    size += len(value.encode())
                    fragments.append(value)
                    if size > MAX_BYTES:
                        raise ValueError("Oversized completion")
                    await emit(sink, value)
            calls = delta.get("tool_calls") or []
            if not isinstance(calls, list) or (calls and finish is not None):
                raise ValueError("Invalid or late tools")
            for call in calls:
                index = call.get("index") if isinstance(call, dict) else None
                if type(index) is not int or not 0 <= index < 100:
                    raise ValueError("Invalid tool index")
                target = tools.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("type", "function") != "function":
                    raise ValueError("Unsupported tool type")
                for field in ("id",):
                    value = call.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError("Invalid tool identifier")
                    target[field] += value
                    size += len(value.encode())
                function = call.get("function", {})
                if not isinstance(function, dict):
                    raise ValueError("Invalid tool function")
                for field in ("name", "arguments"):
                    value = function.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError("Invalid tool fragment")
                    target["function"][field] += value
                    size += len(value.encode())
            if size > MAX_BYTES:
                raise ValueError("Oversized completion")
            reason = choice.get("finish_reason")
            if reason is not None:
                if finish is not None or reason not in ("stop", "length", "tool_calls"):
                    raise ValueError("Invalid finish reason")
                finish = reason
    if data or not done or finish is None:
        raise ValueError("Incomplete SSE")
    if tools and sorted(tools) != list(range(len(tools))):
        raise ValueError("Missing tool index")
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "".join(text),
                    "reasoning_content": "".join(thinking),
                    "tool_calls": list(tools.values()),
                },
                "finish_reason": finish,
            }
        ],
        "usage": usage,
    }
