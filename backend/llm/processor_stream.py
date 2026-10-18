"""Collect private OpenAI-compatible SSE; publish only validated presentations."""

import json

MAX_BYTES = 8 * 1024**2


async def collect_processor_stream(lines):
    fragments, data = [], []
    frame_size = output_size = 0
    finish, usage, done = None, None, False
    metadata = {}
    async for line in lines:
        if len(line.encode()) > MAX_BYTES:
            raise ValueError("Oversized processor SSE line")
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
            frame_size += len(line.encode())
            if frame_size > MAX_BYTES:
                raise ValueError("Oversized processor SSE frame")
        elif not line and data:
            raw = "\n".join(data)
            data, frame_size = [], 0
            if done:
                raise ValueError("Processor data after DONE")
            if raw == "[DONE]":
                done = True
                continue
            chunk = json.loads(raw)
            if not isinstance(chunk, dict) or "error" in chunk:
                raise ValueError("Invalid processor SSE chunk")
            for key in ("id", "model", "created"):
                if key in chunk:
                    metadata[key] = chunk[key]
            choices = chunk.get("choices")
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError("Expected a single streaming choice")
            if chunk.get("usage") is not None:
                if not isinstance(chunk["usage"], dict):
                    raise ValueError("Invalid streaming usage")
                usage = chunk["usage"]
            if not choices:
                continue
            choice = choices[0]
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ValueError("Invalid streaming choice index")
            delta = choice.get("delta")
            if not isinstance(delta, dict) or delta.get("reasoning_content") or delta.get("tool_calls"):
                raise ValueError("Unexpected thinking or tools in Off Formatter")
            text = delta.get("content")
            if text is not None:
                if not isinstance(text, str) or (finish is not None and text):
                    raise ValueError("Invalid streaming content")
                output_size += len(text.encode())
                if output_size > MAX_BYTES:
                    raise ValueError("Oversized processor output")
                fragments.append(text)
            reason = choice.get("finish_reason")
            if reason is not None:
                if finish is not None or reason not in ("stop", "length"):
                    raise ValueError("Invalid streaming finish reason")
                finish = reason
    if data or not done or finish is None or usage is None:
        raise ValueError("Incomplete processor stream or missing usage")
    return {
        **metadata,
        "choices": [
            {"message": {"role": "assistant", "content": "".join(fragments)}, "finish_reason": finish}
        ],
        "usage": usage,
    }
