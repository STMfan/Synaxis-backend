"""Anthropic SSE assembly with separate text and provider-thinking sinks."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

TextSink = Callable[[str], Awaitable[None]]


class StreamProtocolError(ValueError):
    pass


async def sse_events(lines: AsyncIterator[str]):
    data, size = [], 0
    async for line in lines:
        if not line:
            if data:
                try:
                    event = json.loads("\n".join(data))
                    if not isinstance(event, dict):
                        raise ValueError()
                except ValueError as exc:
                    raise StreamProtocolError("Invalid SSE JSON") from exc
                yield event
            data, size = [], 0
        elif line.startswith("data:"):
            size += len(line)
            if size > 8 * 1024**2:
                raise StreamProtocolError("Oversized SSE frame")
            data.append(line[5:].lstrip(" "))
        elif len(line) > 8 * 1024**2:
            raise StreamProtocolError("Oversized SSE line")
    if data:
        raise StreamProtocolError("Unterminated SSE frame")


async def assemble(events, on_text: TextSink | None = None, on_thinking: TextSink | None = None):
    message, blocks, inputs, closed = None, {}, {}, set()
    fragments, input_sizes = {}, {}
    done, characters, final_usage = False, 0, False
    async for event in events:
        kind = event.get("type")
        if kind == "ping":
            continue
        if kind == "error":
            raise StreamProtocolError("Provider stream error")
        if done:
            raise StreamProtocolError("Events after message_stop")
        if kind == "message_start":
            if message is not None or not isinstance(event.get("message"), dict):
                raise StreamProtocolError("Invalid message_start")
            message = event["message"]
            if message.get("content") != [] or not isinstance(message.get("usage"), dict):
                raise StreamProtocolError("Invalid initial message")
            if type(message["usage"].get("input_tokens")) is not int:
                raise StreamProtocolError("Missing stream input usage")
        elif message is None:
            raise StreamProtocolError("Missing message_start")
        elif kind == "content_block_start":
            index, block = event.get("index"), event.get("content_block")
            if type(index) is not int or index != len(blocks) or not isinstance(block, dict):
                raise StreamProtocolError("Invalid block start")
            if block.get("type") not in (
                "text",
                "thinking",
                "tool_use",
                "server_tool_use",
                "web_search_tool_result",
            ):
                raise StreamProtocolError("Unsupported content block")
            blocks[index] = block
            if block["type"] in ("tool_use", "server_tool_use"):
                inputs[index], input_sizes[index] = [], 0
            elif block["type"] in ("text", "thinking"):
                field = block["type"]
                value = block.get(field, "")
                if not isinstance(value, str):
                    raise StreamProtocolError("Invalid initial content")
                characters += len(value)
                if characters > 8 * 1024**2:
                    raise StreamProtocolError("Oversized message")
                sink = on_text if field == "text" else on_thinking
                if value and sink:
                    await sink(value)
        elif kind in ("content_block_delta", "content_block_stop"):
            index = event.get("index")
            if type(index) is not int or index not in blocks or index in closed:
                raise StreamProtocolError("Invalid block index")
            block = blocks[index]
            if kind == "content_block_stop":
                for (part_index, field), parts in fragments.items():
                    if part_index == index:
                        block[field] = "".join(parts)
                if index in inputs and inputs[index]:
                    try:
                        block["input"] = json.loads("".join(inputs[index]))
                        if not isinstance(block["input"], dict):
                            raise ValueError()
                    except ValueError as exc:
                        raise StreamProtocolError("Invalid tool JSON") from exc
                closed.add(index)
                continue
            delta = event.get("delta", {})
            value = delta.get("type")
            field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}.get(
                value
            )
            if field:
                target = "text" if field == "text" else "thinking"
                if block["type"] != target or not isinstance(delta.get(field), str):
                    raise StreamProtocolError("Wrong content delta")
                fragments.setdefault((index, field), [block.get(field, "")]).append(delta[field])
                characters += len(delta[field])
                if characters > 8 * 1024**2:
                    raise StreamProtocolError("Oversized message")
                sink = on_text if field == "text" else on_thinking if field == "thinking" else None
                if delta[field] and sink:
                    await sink(delta[field])
            elif (
                value == "input_json_delta" and index in inputs and isinstance(delta.get("partial_json"), str)
            ):
                inputs[index].append(delta["partial_json"])
                input_sizes[index] += len(delta["partial_json"])
                if input_sizes[index] > 1_000_000:
                    raise StreamProtocolError("Oversized tool input")
            elif (
                value == "citations_delta"
                and block["type"] == "text"
                and isinstance(delta.get("citation"), dict)
            ):
                block.setdefault("citations", []).append(delta["citation"])
            else:
                raise StreamProtocolError("Unsupported delta")
        elif kind == "message_delta":
            if len(closed) != len(blocks) or not isinstance(event.get("delta"), dict):
                raise StreamProtocolError("Invalid message_delta")
            message.update(event["delta"])
            usage = event.get("usage", {})
            if not isinstance(usage, dict):
                raise StreamProtocolError("Invalid stream usage")
            message["usage"].update(usage)
            final_usage = type(usage.get("output_tokens")) is int
        elif kind == "message_stop":
            if len(closed) != len(blocks) or not message.get("stop_reason") or not final_usage:
                raise StreamProtocolError("Incomplete stream")
            done = True
        else:
            raise StreamProtocolError("Unknown SSE event")
    if not done:
        raise StreamProtocolError("Missing message_stop")
    message["content"] = [blocks[i] for i in range(len(blocks))]
    return message
