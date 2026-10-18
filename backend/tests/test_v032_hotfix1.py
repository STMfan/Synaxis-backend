"""Production regressions: TeX, text-only formatting, rich recovery and resume."""

import json

import httpx
import pytest
from conftest import formatted, llm_text, plan_payload, settle

from core.presentation import parse_expert, recover_presentation, validate_presentation
from domain.errors import HarnessError
from llm.contracts import CallContext
from llm.processor_stream import collect_processor_stream
from llm.siliconflow import SiliconFlowClient
from storage.database import Database

IMAGE = "a" * 32
QUESTION = {"type": "question", "id": "q1", "prompt": "明白了吗？", "options": []}


def rich_answer(formula=r"a\xlongequal{\text{通分}}b"):
    return json.dumps(
        {
            "version": "1.0",
            "blocks": [
                {"type": "markdown", "text": "推导：\\[" + formula + "\\]"},
                {"type": "localfile", "artifact_id": IMAGE, "label": "函数图.png"},
                {"type": "markdown", "text": "看图验证以上结论。"},
            ],
        },
        ensure_ascii=False,
    )


def test_production_ams_symbol_no_longer_triggers_formatter():
    text = r"图像关于 \((0,a)\) 对称。\(\blacksquare\)"
    assert parse_expert(text, []).blocks[0].text == text


def test_annotated_equals_is_compatible_and_image_stays_between_text():
    value = parse_expert(rich_answer(), [IMAGE])
    assert [b.type for b in value.blocks] == ["markdown", "localfile", "markdown"]
    assert value.blocks[0].text == r"推导：\[a\overset{\text{通分}}{=}b\]"
    assert value.blocks[1].artifact_id == IMAGE


def test_only_math_is_normalized_and_nested_labels_are_preserved():
    code = "```python\nvalue = r'\\xlongequal{label}'\n```"
    text = code + "\n" + r"\(x\xlongequal{\frac{a}{b}}y\)"
    output = parse_expert(text, []).blocks[0].text
    assert output.startswith(code)
    assert output.endswith(r"\(x\overset{\frac{a}{b}}{=}y\)")
    assert parse_expert("`\\xlongequal{label}`", []).blocks[0].text == "`\\xlongequal{label}`"
    source = '{"blocks":[{"type":"latex","source":"a\\\\xlongequal{b}c"}]}'
    assert parse_expert(source, []).blocks[0].source == r"a\overset{b}{=}c"


@pytest.mark.parametrize(
    "source",
    [r"\(a\xlongequal\)", r"\(a\xlongequal{\href{bad}{x}}b\)", r"\(a\xlongequal{\xlongequal{b}}c\)"],
)
def test_normalization_still_rejects_invalid_or_unsafe_tex(source):
    with pytest.raises((ValueError, HarnessError)):
        parse_expert(source, [])


def test_formatter_failure_quotes_only_invalid_leaf_and_keeps_order():
    value = recover_presentation(rich_answer(r"a\unknown{x}b"), [IMAGE], QUESTION)
    assert [b.type for b in value.blocks] == ["markdown", "localfile", "markdown", "question"]
    assert value.blocks[0].text.startswith("```text")
    assert value.blocks[2].text == "看图验证以上结论。"
    validate_presentation(value, [IMAGE], QUESTION)
    # An unauthorized file is never retained as a downloadable block.
    rejected = recover_presentation(rich_answer(), [], QUESTION)
    assert [b.type for b in rejected.blocks] == ["markdown", "question"]
    assert rejected.blocks[0].text.startswith("```text")


def stream_payload(text, *, usage=True, done=True, reason="stop", thinking=None):
    chunks = [
        {
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text[:3]}, "finish_reason": None}
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text[3:], "reasoning_content": thinking},
                    "finish_reason": reason,
                }
            ]
        },
    ]
    if usage:
        chunks.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    content = b"".join(("data: " + json.dumps(c, ensure_ascii=False) + "\r\n\r\n").encode() for c in chunks)
    return content + (b"data: [DONE]\r\n\r\n" if done else b"")


async def test_private_formatter_stream_audited_after_completion(settings):
    db = Database(settings.db_path)
    db.initialize()
    bodies = []
    text = '{"blocks":[{"type":"markdown","text":"中文答案"}]}'

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, content=stream_payload(text), headers={"content-type": "text/event-stream"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        reply = await SiliconFlowClient(settings, db, http).complete(
            CallContext(1, "s", "formatter"),
            system="JSON",
            messages=[{"role": "user", "content": "x"}],
            model=settings.process_model,
            effort="off",
        )
    assert reply["content"][0]["text"] == text and reply["stop_reason"] == "end_turn"
    assert bodies[0]["stream"] and bodies[0]["stream_options"]["include_usage"]
    assert db.usage(1)["items"][0]["output_tokens"] == 20


@pytest.mark.parametrize(
    "options", [{"done": False}, {"usage": False}, {"thinking": "private"}, {"reason": "tool_calls"}]
)
async def test_incomplete_or_unexpected_formatter_stream_is_not_accepted(options):
    async def lines():
        for line in stream_payload("答案", **options).decode().splitlines():
            yield line

    with pytest.raises(ValueError):
        await collect_processor_stream(lines())


async def test_image_question_does_not_send_image_to_formatter(app_factory):
    from test_client_api import student_image

    async with app_factory([plan_payload(), llm_text("<b>解释文字</b>"), formatted("解释文字")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (
            await client.post("/v1/sessions", json={"message": "解释图片", "images": [student_image()]})
        ).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        formatter = requests[-1]["body"]
        assert all(isinstance(m["content"], str) for m in formatter["messages"])
        data = json.loads(formatter["messages"][-1]["content"])
        assert set(data) == {"answer", "question", "artifact_ids", "validation_error"}
        assert "images" not in data and "base64" not in json.dumps(formatter)


async def test_completed_stream_resume_is_clean_and_does_not_create_calls(app_factory):
    async with app_factory([plan_payload(), llm_text("已完成")]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "测试续传"})).json()["id"]
        await settle(app, sid)
        events = app.state.db.events(sid)
        cursor = next(e["id"] for e in events if e["kind"] == "output_start")
        response = await client.get(f"/v1/sessions/{sid}/events?after={cursor}")
        assert response.status_code == 200 and "event: completed" in response.text
        assert f"id: {cursor}\n" not in response.text
        last = events[-1]["id"]
        empty = await client.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": str(last)})
        assert empty.status_code == 200 and empty.text == ""
        assert (await client.get(f"/v1/sessions/{sid}")).json()["status"] == "completed"
        assert len(requests) == 2
