"""0.3 regressions: early deltas, strict formatting, optional Max and budget."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from conftest import formatted, llm_text, plan_payload, settle, stream_frames

from core.configuration import CONFIG_DIR, EXPERTS, MODELS, Expert, load_config
from core.presentation import literal_fallback, parse_expert, validate_markdown
from core.settings import ROOT
from domain.errors import HarnessError

FENCE = chr(96) * 3


def plan(expert="Euclid", subject="math", **options):
    payload = plan_payload(expert, subject)
    value = json.loads(payload["content"][0]["text"])
    value.update(options)
    payload["content"][0]["text"] = json.dumps(value)
    return payload


class GatedStream(httpx.AsyncByteStream):
    def __init__(self, frames, release, entered, fail=False):
        self.frames, self.release, self.entered, self.fail = frames, release, entered, fail

    async def __aiter__(self):
        for frame in self.frames:
            if b"message_delta" in frame:
                self.entered.set()
                await self.release.wait()
                if self.fail:
                    raise httpx.ReadError("broken connection")
            # Exercise UTF-8 and SSE reconstruction across arbitrary byte chunks.
            for position in range(0, len(frame), 7):
                yield frame[position : position + 7]


async def test_delta_before_completion_and_resume(app_factory):
    release, entered = asyncio.Event(), asyncio.Event()
    payload = llm_text("定义：函数对每个输入指定唯一输出。")
    payload["content"].insert(
        0, {"type": "thinking", "thinking": "secret-reasoning", "signature": "private-signature"}
    )
    response = httpx.Response(
        200,
        stream=GatedStream(stream_frames(payload), release, entered),
        headers={"content-type": "text/event-stream"},
    )
    async with app_factory([plan_payload(), response]) as (app, client, requests, remaining):
        sid = (await client.post("/v1/sessions", json={"message": "函数是什么？"})).json()["id"]
        await asyncio.wait_for(entered.wait(), 2)
        events = (await client.get(f"/v1/sessions/{sid}/events/history")).json()["items"]
        delta = next(e for e in events if e["event"] == "output_delta")
        assert delta["data"]["text"] == payload["content"][1]["text"]
        assert not app.state.db.session(sid)["state"]["presentations"]
        assert "secret-reasoning" in json.dumps(events) and "private-signature" not in json.dumps(events)
        assert app.state.db.usage(1)["items"][0]["status"] == "pending"
        release.set()
        assert (await settle(app, sid))["status"] == "completed"
        resumed = await client.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": str(delta["id"])})
        assert "event: output_delta" not in resumed.text and "event: presentation" in resumed.text
        assert len(requests) == 2 and not remaining
        assert requests[1]["body"]["max_tokens"] == 393216
        assert requests[0]["body"]["max_tokens"] == 393216


@pytest.mark.parametrize("cancel", [False, True])
async def test_stream_failure_keeps_budget_and_no_formatter_retry(app_factory, cancel):
    release, entered = asyncio.Event(), asyncio.Event()
    response = httpx.Response(
        200,
        stream=GatedStream(stream_frames(llm_text("部分答案")), release, entered, fail=True),
        headers={"content-type": "text/event-stream"},
    )
    async with app_factory([plan_payload(), response]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "复杂题"})).json()["id"]
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            await client.post(f"/v1/sessions/{sid}/cancel")
        else:
            release.set()
            await settle(app, sid)
        row = app.state.db.session(sid)
        assert row["status"] == ("cancelled" if cancel else "failed")
        assert not row["state"]["presentations"] and len(requests) == 2
        assert (
            next(e for e in app.state.db.events(sid) if e["kind"] == "output_end")["data"]["status"]
            == "aborted"
        )
        budget = app.state.db.budget_snapshot(sid, 1, app.state.settings)
        assert Decimal(budget["uncertain"]) > 3 and Decimal(budget["reserved"]) == 0


@pytest.mark.parametrize("remove_stop", [True, False])
async def test_invalid_stream_fails_without_retry(app_factory, remove_stop):
    frames = stream_frames(llm_text("x"))
    content = b"".join(frames[:-1]) if remove_stop else b'event: error\ndata: {"type":"error"}\n\n'
    response = httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})
    async with app_factory([plan_payload(), response]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        assert (await settle(app, sid))["state"]["error"] == "PROVIDER_INVALID_STREAM"
        assert len(requests) == 2


async def test_structured_expert_skips_formatter_and_invalid_uses_it(app_factory):
    answer = {
        "blocks": [
            {"type": "latex", "source": r"x=\frac{1}{2}"},
            {"type": "svg", "source": '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>'},
        ]
    }
    async with app_factory(
        [plan_payload(), llm_text(json.dumps(answer)), llm_text("<b>一半</b>"), formatted("一半")]
    ) as (app, client, requests, remaining):
        sid = (await client.post("/v1/sessions", json={"message": "解答"})).json()["id"]
        row = await settle(app, sid)
        assert row["state"]["presentations"][0]["blocks"][0]["type"] == "latex" and len(requests) == 2
        await client.post(f"/v1/sessions/{sid}/messages", json={"message": "解释"})
        row = await settle(app, sid)
        assert row["state"]["presentations"][1]["blocks"][0]["text"] == "一半"
        assert [r["stage"] for r in app.state.db.usage(1)["items"]] == [
            "formatter",
            "expert",
            "expert",
            "planner",
        ]
        assert len(requests) == 4 and not remaining


@pytest.mark.parametrize("name", list(EXPERTS))
async def test_all_experts_can_request_max_then_default_on_followup(app_factory, name):
    expert = EXPERTS[name]
    request_max = {
        "content": [
            {
                "type": "tool_use",
                "id": "upgrade",
                "name": "RequestMaxThinking",
                "input": {"reason": "需要逐步核验多种条件"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    async with app_factory(
        [plan(name, expert.subjects[0]), request_max, llm_text("核验后结论"), llm_text("简短解释")]
    ) as (app, client, requests, remaining):
        sid = (
            await client.post("/v1/sessions", json={"message": "多步问题", "subject": expert.subjects[0]})
        ).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        first, upgraded = requests[1]["body"], requests[2]["body"]
        assert first["thinking"]["type"] == ("disabled" if name == "Poiesis" else "enabled")
        assert upgraded["output_config"]["effort"] == "max"
        assert "RequestMaxThinking" not in [t["name"] for t in upgraded["tools"]]
        await client.post(
            f"/v1/sessions/{sid}/messages", json={"message": "再解释", "allow_max_thinking": False}
        )
        assert (await settle(app, sid))["status"] == "completed"
        follow = requests[3]["body"]
        assert follow.get("output_config", {}).get("effort") == (None if name == "Poiesis" else "low")
        assert "RequestMaxThinking" not in [t["name"] for t in follow["tools"]]
        assert not remaining


@pytest.mark.parametrize("name,subject", [("Turing", "math"), ("Euclid", "math"), ("Nomos", "physics")])
async def test_planner_can_select_max_for_extreme_stem(app_factory, name, subject):
    async with app_factory(
        [
            plan(name, subject, thinking="max", complexity="extreme", max_reason="多条件综合建模"),
            llm_text("必要推导"),
        ]
    ) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "复杂综合题"})).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        assert requests[0]["body"]["thinking"] == {"type": "disabled"}
        assert "output_config" not in requests[0]["body"]
        assert requests[1]["body"]["output_config"]["effort"] == "max"


@pytest.mark.parametrize(
    "options,body",
    [
        ({"expert": "Nexus", "subject": "english", "complexity": "extreme", "max_reason": "复杂"}, {}),
        ({"complexity": "hard", "max_reason": "复杂"}, {}),
        ({"complexity": "extreme", "max_reason": ""}, {}),
        ({"complexity": "extreme", "max_reason": "复杂"}, {"allow_max_thinking": False}),
    ],
)
async def test_planner_max_policy_rejects_without_expert_charge(app_factory, options, body):
    async with app_factory([plan(thinking="max", **options)]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "问题", **body})).json()["id"]
        assert (await settle(app, sid))["state"]["error"] == "PLANNER_INVALID_OUTPUT"
        assert len(requests) == 1


@pytest.mark.parametrize("text", [r"公式 \(\frac{1}{2}\)", "简短定义", FENCE + "python\nprint(1)\n" + FENCE])
def test_renderable_markdown(text):
    assert parse_expert(text, []).blocks[0].text == text


@pytest.mark.parametrize(
    "text",
    [
        r"\(x",
        r"\(\frac{1}{2\)",
        r"\(\badcommand{x}\)",
        FENCE + "python\nx",
        "<script>x</script>",
        "[x](java&#115;cript:alert(1))",
        "![image](https://a)",
        '{"blocks":[]}',
        '{"blocks":[{"type":"question","id":"x","prompt":"invented"}]}',
    ],
)
def test_invalid_expert_requires_formatter(text):
    with pytest.raises((ValueError, HarnessError)):
        parse_expert(text, [])
    result = literal_fallback(text)
    validate_markdown(result.blocks[0].text)
    assert text in result.blocks[0].text


def test_large_markdown_is_not_silently_truncated():
    text = "很难问题的必要论证。" * 10000
    assert len(text) > 50000 and parse_expert(text, []).blocks[0].text == text


def test_json_configuration_external_and_validated(tmp_path):
    assert CONFIG_DIR == ROOT / "config/ExpertsPrompts"
    assert MODELS.main.effort == "low" and MODELS.literature.effort == "off"
    assert MODELS.expert_max_tokens == 393216
    value = json.loads((CONFIG_DIR / "Euclid.json").read_text(encoding="utf-8"))
    value["instructions"] = "管理员修改的教学风格"
    (tmp_path / "Euclid.json").write_text(json.dumps(value), encoding="utf-8")
    assert load_config("Euclid", Expert, tmp_path).instructions == value["instructions"]
    value["effort"] = "typo"
    (tmp_path / "Euclid.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config("Euclid", Expert, tmp_path)


async def test_missing_stream_usage_does_not_look_free(app_factory):
    frames = stream_frames(llm_text("answer"))
    frames[-2] = (
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{}}\n\n'
    )
    response = httpx.Response(200, content=b"".join(frames), headers={"content-type": "text/event-stream"})
    async with app_factory([plan_payload(), response]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        assert (await settle(app, sid))["state"]["error"] == "PROVIDER_INVALID_STREAM"
        assert app.state.db.usage(1)["items"][0]["cost"] is None
        assert Decimal(app.state.db.budget_snapshot(sid, 1, app.state.settings)["uncertain"]) > 3


async def test_literary_384k_limit_is_reduced_to_available_budget(app_factory):
    async with app_factory([plan("Poiesis", "chinese"), llm_text("比喻是以相似的事物作比。")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (
            await client.post("/v1/sessions", json={"message": "什么是比喻？", "subject": "chinese"})
        ).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        body = requests[1]["body"]
        assert 256 <= body["max_tokens"] < 393216
        assert body["thinking"]["type"] == "disabled"
        budget = app.state.db.budget_snapshot(sid, 1, app.state.settings)
        assert Decimal(budget["spent"]) < 5 and Decimal(budget["reserved"]) == 0


@pytest.mark.parametrize("source", [r"\frac", r"\frac{1}", r"\left(x", r"\right)x\left("])
def test_incomplete_common_mathjax_commands_require_formatter(source):
    with pytest.raises(HarnessError):
        parse_expert(json.dumps({"blocks": [{"type": "latex", "source": source}]}), [])


def test_left_arrow_is_not_a_left_delimiter():
    assert parse_expert(json.dumps({"blocks": [{"type": "latex", "source": r"A\leftarrow B"}]}), [])


def test_unclosed_html_requires_formatter():
    with pytest.raises(HarnessError):
        parse_expert(json.dumps({"blocks": [{"type": "html", "source": "<div><span>x</span>"}]}), [])
