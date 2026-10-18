"""0.3.2: discipline methods, inline artifacts, reasoning traces and time bands."""

import asyncio
import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from conftest import llm_text, plan_payload, settle, stream_frames
from test_v030 import GatedStream

from core.configuration import EXPERTS, SHARED, PricingConfig
from core.prompts import expert_prompt
from core.round_log import RoundLog
from core.settings import Settings
from core.version import VERSION
from llm.contracts import CallContext
from llm.deepseek import DeepSeekClient
from llm.streaming import assemble
from storage.artifacts import ArtifactStore
from storage.budget import cny_cost, reservation_cost
from storage.database import Database, now
from storage.pricing import price_snapshot, pricing_band


@pytest.mark.parametrize(
    "at,band",
    [
        ("2026-10-08T08:59:59+08:00", "off_peak"),
        ("2026-10-08T09:00:00+08:00", "peak"),
        ("2026-10-08T11:59:59+08:00", "peak"),
        ("2026-10-08T12:00:00+08:00", "off_peak"),
        ("2026-10-08T13:59:59+08:00", "off_peak"),
        ("2026-10-08T14:00:00+08:00", "peak"),
        ("2026-10-08T17:59:59+08:00", "peak"),
        ("2026-10-08T18:00:00+08:00", "off_peak"),
        ("2026-10-08T01:00:00Z", "peak"),
        ("2026-10-08T02:00:00+01:00", "peak"),
        ("2026-10-10T10:00:00+08:00", "off_peak"),  # make-up Saturday
        ("2026-10-11T10:00:00+08:00", "off_peak"),
        ("2026-10-07T10:00:00+08:00", "off_peak"),  # national holiday
        ("2026-02-23T15:00:00+08:00", "off_peak"),
        ("2026-05-05T15:00:00+08:00", "off_peak"),
    ],
)
def test_beijing_peak_boundaries_weekends_and_holidays(settings, at, band):
    settings.pricing_mode = "auto"
    assert pricing_band(settings, datetime.fromisoformat(at)) == band


def test_auto_default_manual_override_and_timezone_required(settings):
    assert Settings(_env_file=None).pricing_mode == "auto"
    assert pricing_band(settings, datetime.fromisoformat("2026-10-07T00:00:00Z")) == "peak"
    settings.pricing_mode = "off_peak"
    assert pricing_band(settings, datetime.fromisoformat("2026-10-08T01:00:00Z")) == "off_peak"
    with pytest.raises(ValueError):
        pricing_band(settings, datetime(2026, 10, 8))


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro", "deepseek-v4-flash"])
def test_cached_and_uncached_off_peak_costs_but_peak_reservation(settings, model):
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 10,
        "output_tokens": 50,
    }
    settings.pricing_mode = "auto"
    peak = price_snapshot(settings, model, datetime.fromisoformat("2026-10-08T10:00:00+08:00"))
    off = price_snapshot(settings, model, datetime.fromisoformat("2026-10-08T18:00:00+08:00"))
    assert cny_cost(settings, model, usage, snapshot=off) * 2 == cny_cost(
        settings, model, usage, snapshot=peak
    )
    body = {"messages": [], "max_tokens": 1024}
    reserve = reservation_cost(settings, model, body)
    settings.pricing_mode = "peak"
    assert reservation_cost(settings, model, body) == reserve
    # Existing historical pricing keeps its peak interpretation.
    assert cny_cost(settings, model, usage) == cny_cost(settings, model, usage, snapshot=peak)
    assert ":off_peak:" in off.basis and "request_start=" in off.basis and ";rates=" in off.basis


async def test_call_keeps_start_band_across_boundary_and_rate_change(settings, monkeypatch):
    settings.pricing_mode = "auto"
    db = Database(settings.db_path)
    db.initialize()
    moment = [datetime.fromisoformat("2026-10-08T08:59:59+08:00")]
    monkeypatch.setattr("storage.pricing.utc_now", lambda: moment[0])
    captured = price_snapshot(settings, settings.main_model)

    def handler(request):
        moment[0] = datetime.fromisoformat("2026-10-08T09:00:02+08:00")
        settings.price_rates_cny = {**settings.price_rates_cny, settings.main_model: [10, 10, 10]}
        return httpx.Response(200, json=llm_text("done"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await DeepSeekClient(settings, db, http).complete(
            CallContext(1, "time-test", "expert"),
            system="test",
            messages=[{"role": "user", "content": []}],
            model=settings.main_model,
            effort="off",
            max_tokens=1024,
        )
    row = db.usage(1)["items"][0]
    assert row["pricing_basis"] == captured.basis
    assert Decimal(row["cost"]) == cny_cost(
        settings, settings.main_model, llm_text("")["usage"], snapshot=captured
    )
    budget = db.budget_snapshot("time-test", 1, settings)
    assert budget["spent"] == row["cost"] and Decimal(budget["reserved"]) == 0


async def test_reasoning_before_answer_completion_replay_and_owner_download(app_factory):
    release, entered = asyncio.Event(), asyncio.Event()
    payload = llm_text("结论正确。")
    payload["content"].insert(
        0, {"type": "thinking", "thinking": "检查边界与符号。", "signature": "never-export"}
    )
    response = httpx.Response(
        200,
        stream=GatedStream(stream_frames(payload), release, entered),
        headers={"content-type": "text/event-stream"},
    )
    async with app_factory([plan_payload(), response]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "验证"})).json()["id"]
        await asyncio.wait_for(entered.wait(), 2)
        page = (await client.get(f"/v1/sessions/{sid}/events/history")).json()
        thought = next(e for e in page["items"] if e["event"] == "reasoning_delta")
        assert thought["data"]["text"] == "检查边界与符号。"
        assert thought["data"]["sequence"] == 1
        answer = next(e for e in page["items"] if e["event"] == "output_delta")
        assert thought["data"]["stream_id"] != answer["data"]["stream_id"]
        assert "never-export" not in json.dumps(page)
        assert not app.state.db.session(sid)["state"]["presentations"]
        release.set()
        row = await settle(app, sid)
        assert row["status"] == "completed"
        assert row["state"]["messages"][1]["content"][0] == payload["content"][0]
        replay = await client.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": str(thought["id"])})
        assert "event: reasoning_delta" not in replay.text and "event: reasoning_end" in replay.text
        log_id = row["state"]["presentations"][0]["blocks"][-1]["artifact_id"]
        download = await client.get(f"/v1/artifacts/{log_id}")
        assert "[provider_thinking]" in download.text and "检查边界与符号。" in download.text
        assert "结论正确。" in download.text and "never-export" not in download.text
        assert requests[1]["body"]["stream"] is True
        bob = (
            await client.post("/v1/auth/token", data={"username": "bob", "password": "strong-test-password"})
        ).json()
        assert (
            await client.get(
                f"/v1/artifacts/{log_id}", headers={"Authorization": "Bearer " + bob["access_token"]}
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/v1/sessions/{sid}/events/history",
                headers={"Authorization": "Bearer " + bob["access_token"]},
            )
        ).status_code == 404


@pytest.mark.parametrize("cancel", [False, True])
async def test_aborted_reasoning_remains_in_partial_log_without_valid_answer(app_factory, cancel):
    release, entered = asyncio.Event(), asyncio.Event()
    payload = llm_text("未完成")
    payload["content"].insert(0, {"type": "thinking", "thinking": "部分检查", "signature": "no-leak"})
    response = httpx.Response(
        200,
        stream=GatedStream(stream_frames(payload), release, entered, fail=True),
        headers={"content-type": "text/event-stream"},
    )
    async with app_factory([plan_payload(), response]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "问题"})).json()["id"]
        await asyncio.wait_for(entered.wait(), 2)
        if cancel:
            await client.post(f"/v1/sessions/{sid}/cancel")
        else:
            release.set()
        row = await settle(app, sid)
        assert not row["state"]["presentations"]
        events = (await client.get(f"/v1/sessions/{sid}/events/history")).json()["items"]
        end = next(e for e in events if e["event"] == "reasoning_end")
        assert end["data"]["status"] == "aborted"
        artifact = next(e for e in events if e["event"] == "artifact")["data"]
        assert artifact["status"] == "aborted"
        log = (await client.get(f"/v1/artifacts/{artifact['artifact_id']}")).text
        assert "部分检查" in log and "status=aborted" in log and "no-leak" not in log
        assert Decimal(app.state.db.budget_snapshot(sid, 1, app.state.settings)["uncertain"]) > 0


async def test_thinking_initial_blocks_and_multiple_fragments_never_stream_signatures():
    events = [
        {"type": "message_start", "message": {"content": [], "usage": {"input_tokens": 1}}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "初", "signature": "opaque"},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "步"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signature-tail"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]

    async def source():
        for item in events:
            yield item

    chunks = []

    async def receive(text):
        chunks.append(text)

    result = await assemble(source(), on_thinking=receive)
    assert chunks == ["初", "步"]
    assert result["content"][0]["thinking"] == "初步"
    assert result["content"][0]["signature"] == "opaquesignature-tail"


def test_round_log_utf8_byte_bound_and_explicit_truncation(settings):
    settings.artifact_bytes = 1024
    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO users(username,password_hash,created_at) VALUES('test','hash',?)", (now(),))
    sid = db.create_session(1, {})
    trace = RoundLog(CallContext(1, sid, "expert"), 1, "low", ArtifactStore(settings, db), db)
    trace.capture("thinking", "中" * 1000)
    trace.capture("output", "末" * 1000)
    file = trace.finish("received")
    raw = trace.artifacts.read(trace.ctx, file.artifact_id)
    assert len(raw) <= 1024
    assert "truncated" in raw.decode("utf-8")


async def test_off_expert_no_reasoning_events_and_explicit_unavailable_log(app_factory):
    async with app_factory([plan_payload("Poiesis", "chinese"), llm_text("意象是承载情感的具体形象。")]) as (
        app,
        client,
        _,
        _,
    ):
        sid = (
            await client.post("/v1/sessions", json={"message": "意象是什么", "subject": "chinese"})
        ).json()["id"]
        row = await settle(app, sid)
        assert not any(e["kind"].startswith("reasoning_") for e in app.state.db.events(sid))
        file = row["state"]["presentations"][0]["blocks"][-1]
        assert "No thinking returned" in (await client.get(f"/v1/artifacts/{file['artifact_id']}")).text


async def test_image_artifact_stays_between_text_without_formatter(app_factory):
    async with app_factory([plan_payload(skills=["Render"])]) as (app, client, requests, remaining):
        # Create the artifact inside the tool round, then use its real ID.
        tool = llm_text("")
        tool["content"] = [
            {"type": "thinking", "thinking": "先制图", "signature": "s"},
            {"type": "tool_use", "id": "draw", "name": "Render", "input": {}},
        ]

        async def execute(ctx, *args, **kwargs):
            return app.state.artifacts.write(ctx, "render-output.png", "image/png", b"fixture")

        app.state.executor.execute = execute

        async def answer(request):
            body = json.loads(request.content)
            artifact = json.loads(body["messages"][-1]["content"][0]["content"])["artifact_id"]
            return llm_text(
                json.dumps(
                    {
                        "blocks": [
                            {"type": "markdown", "text": "先看函数图："},
                            {"type": "localfile", "artifact_id": artifact, "label": "函数图"},
                            {"type": "markdown", "text": "由图观察，再用解析式验证。"},
                        ]
                    }
                )
            )

        remaining.extend([tool, answer])
        sid = (await client.post("/v1/sessions", json={"message": "画图"})).json()["id"]
        row = await settle(app, sid)
        assert row["status"] == "completed"
        blocks = row["state"]["presentations"][0]["blocks"]
        assert [b["type"] for b in blocks] == ["markdown", "localfile", "markdown", "localfile"]
        assert len(requests) == 3
        assert len([e for e in app.state.db.events(sid) if e["kind"] == "artifact"]) == 2


@pytest.mark.parametrize(
    "name,terms",
    [
        ("Poiesis", ["文本支点", "学生声音"]),
        ("Nexus", ["语域", "主干谓语"]),
        ("Euclid", ["等价", "等可能性"]),
        ("Turing", ["韦达", "斜率不存在"]),
        ("Nomos", ["系统边界", "向心力"]),
        ("Panopt", ["电荷", "自变量"]),
        ("Chronos", ["一手史料", "人民史观"]),
        ("Meridian", ["图例", "空间尺度"]),
    ],
)
def test_specialized_method_and_stable_shared_prefix(name, terms):
    prompt = expert_prompt(name, [])
    prefix = (
        SHARED.common
        + SHARED.base_skills
        + SHARED.response_style
        + SHARED.output_contract
        + SHARED.presentation_contract
        + SHARED.visual_contract
    )
    assert prompt.startswith(prefix)
    assert all(term in EXPERTS[name].instructions for term in terms)
    assert "1024" in prefix and "0.25像素" in prefix and "artifact_id" in prefix


async def test_capabilities_and_typed_reasoning_docs(app_factory):
    async with app_factory() as (_, client, _, _):
        cap = (await client.get("/v1/capabilities")).json()
        assert cap["version"] == VERSION and cap["reasoning_streaming"]
        assert cap["artifact_placement"] == "ordered_blocks"
        assert cap["pricing"]["reservation_band"] == "peak"
        for language in ("zh", "en"):
            schema = (await client.get("/openapi.json?lang=" + language)).json()
            for name in (
                "ReasoningStartEvent",
                "ReasoningDeltaEvent",
                "ReasoningEndEvent",
                "ArtifactEvent",
                "PricingInfo",
            ):
                assert name in schema["components"]["schemas"]


def test_invalid_pricing_calendar_rejected():
    from core.configuration import PRICING

    bad = PRICING.model_dump()
    bad["peak_windows"] = [("14:00", "18:00"), ("09:00", "12:00")]
    with pytest.raises(ValueError):
        PricingConfig.model_validate(bad)
