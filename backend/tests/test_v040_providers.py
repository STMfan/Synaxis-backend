import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from conftest import llm_text, plan_payload, settle, student_image
from pydantic import SecretStr, ValidationError

from auth.security import hash_password
from domain.errors import HarnessError
from domain.schemas import TutorRequest
from llm.contracts import CallContext
from llm.openai_compatible import OpenAICompatibleClient, chat_messages, normalized_completion
from llm.openai_stream import collect_chat_stream
from llm.providers import TextProvider, key_for
from llm.router import RoutedClient
from storage.database import Database, now


def provider():
    return TextProvider(
        protocol="openai_chat",
        base_url="https://compatible.test/v1",
        output_limits={"teacher": 32768},
        context_limits={"teacher": 131072},
        price_rates_cny={"teacher": [0.1, 1, 2]},
        reasoning_parameter="reasoning_effort",
        reasoning_values={"off": "none", "low": "low", "high": "high", "max": "max"},
        supports_json_mode=True,
    )


@pytest.fixture
def generic(settings):
    settings.providers["custom"] = provider()
    settings.main_provider = settings.literature_provider = settings.process_provider = "custom"
    settings.main_model = settings.literature_model = settings.process_model = "teacher"
    settings.text_main_key = settings.text_literature_key = settings.text_process_key = SecretStr(
        "synthetic-generic-key"
    )
    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'test',?,?)",
            (hash_password("synthetic-password"), now()),
        )
    sid = db.create_session(1, TutorRequest(message="synthetic").model_dump())
    return settings, db, CallContext(1, sid, "expert")


def complete_payload(text="result", tools=None, usage=True):
    result = {
        "choices": [
            {
                "message": {"content": text, "tool_calls": tools},
                "finish_reason": "tool_calls" if tools else "stop",
            }
        ]
    }
    if usage:
        result["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        }
    return result


async def lines(frames):
    for value in frames:
        raw = value if isinstance(value, str) else json.dumps(value)
        yield "data: " + raw
        yield ""


async def test_generic_stream_reasoning_tools_usage_and_message_replay():
    text, thinking = [], []
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "derive"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "content": "result",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "AskStudent", "arguments": '{"question_id":'},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"q1"}'}}]},
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30}},
        "[DONE]",
    ]
    payload = await collect_chat_stream(lines(frames), text.append, thinking.append)
    normalized = normalized_completion(payload)
    assert text == ["result"] and thinking == ["derive"]
    assert normalized["content"][-1]["input"] == {"question_id": "q1"}
    history = chat_messages(
        "system",
        [
            {"role": "assistant", "content": normalized["content"]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "student answer"}],
            },
        ],
        provider(),
    )
    assert history[1]["tool_calls"][0]["function"]["name"] == "AskStudent"
    assert "reasoning_content" not in history[1] and history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "call-1"


@pytest.mark.parametrize(
    "frames",
    [
        [{"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}],
        [{"choices": [{"delta": {}, "finish_reason": "content_filter"}]}, "[DONE]"],
        [
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [{"delta": {"content": "late"}, "finish_reason": None}]},
            "[DONE]",
        ],
        [{"error": "private provider error"}, "[DONE]"],
        [{"choices": [{"index": 1, "delta": {}, "finish_reason": "stop"}]}, "[DONE]"],
    ],
)
async def test_generic_rejects_incomplete_or_malformed_stream(frames):
    with pytest.raises(ValueError):
        await collect_chat_stream(lines(frames))


async def test_generic_images_reasoning_parameter_budget_and_audit(generic):
    settings, db, ctx = generic
    captured, text = [], []
    settings.providers["custom"].max_tokens_field = "max_completion_tokens"

    def handler(request):
        captured.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer synthetic-generic-key"
        return httpx.Response(200, json=complete_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleClient(settings, db, http, "main")
        result = await client.complete(
            ctx,
            system="stable prefix",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "synthetic"},
                        {"type": "image", "source": student_image()},
                    ],
                }
            ],
            model="teacher",
            effort="max",
            on_text=text.append,
            max_tokens=1048576,
        )
    assert result["stop_reason"] == "end_turn" and text == ["result"]
    body = captured[0]
    assert body["reasoning_effort"] == "max" and "max_tokens" not in body
    assert body["max_completion_tokens"] <= 32768
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    call = db.usage(1)["items"][0]
    assert Decimal(call["cost"]) == Decimal("0.000104") and "custom" in call["pricing_basis"]
    assert Decimal(db.budget_snapshot(ctx.session_id, 1, settings)["reserved"]) == 0


async def test_unknown_usage_and_cancelled_call_retain_uncertain_budget(generic):
    settings, db, ctx = generic
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=complete_payload(usage=False)))
    ) as http:
        await OpenAICompatibleClient(settings, db, http, "main").complete(
            ctx, system="s", messages=[{"role": "user", "content": "x"}], model="teacher", max_tokens=256
        )
    assert db.usage(1)["items"][0]["cost"] is None
    assert Decimal(db.budget_snapshot(ctx.session_id, 1, settings)["uncertain"]) > 0

    async def cancelled(request):
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancelled)) as http:
        with pytest.raises(asyncio.CancelledError):
            await OpenAICompatibleClient(settings, db, http, "main").complete(
                ctx, system="s", messages=[{"role": "user", "content": "x"}], model="teacher", max_tokens=256
            )
    assert db.usage(1)["items"][0]["status"] == "cancelled"


@pytest.mark.parametrize(
    "case,code",
    [
        ("key", "PROVIDER_NOT_CONFIGURED"),
        ("limits", "PROVIDER_LIMITS_UNKNOWN"),
        ("price", "BUDGET_PRICE_UNKNOWN"),
        ("effort", "PROVIDER_EFFORT_UNSUPPORTED"),
        ("context", "CONTEXT_TOO_LARGE"),
    ],
)
async def test_generic_preflight_never_dispatches_unknown_configuration(generic, case, code):
    settings, db, ctx = generic
    if case == "key":
        settings.text_main_key = SecretStr("")
    if case == "limits":
        settings.providers["custom"].output_limits = {}
    if case == "price":
        settings.providers["custom"].price_rates_cny = {}
    if case == "effort":
        settings.providers["custom"].reasoning_values = {"off": "none"}
    if case == "context":
        settings.providers["custom"].context_limits = {"teacher": 256}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Must not dispatch"))
    ) as http:
        with pytest.raises(HarnessError, match=code):
            await OpenAICompatibleClient(settings, db, http, "main").complete(
                ctx, system="s", messages=[{"role": "user", "content": "x"}], model="teacher", effort="low"
            )
    assert db.usage(1)["items"] == []


async def test_generic_teaching_end_to_end_and_capabilities(app_factory, settings):
    settings.providers["custom"] = provider()
    settings.main_provider = settings.literature_provider = settings.process_provider = "custom"
    settings.main_model = settings.literature_model = settings.process_model = "teacher"
    settings.text_main_key = settings.text_literature_key = settings.text_process_key = SecretStr(
        "synthetic-generic-key"
    )
    async with app_factory(
        [plan_payload(), llm_text("因式分解得 $x=2$ 或 $x=3$。"), llm_text("第二步用零乘积性质。")]
    ) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "x²−5x+6=0", "subject": "math"})).json()[
            "id"
        ]
        assert (await settle(app, sid))["status"] == "completed"
        assert (
            await client.post(f"/v1/sessions/{sid}/messages", json={"message": "第二步呢？"})
        ).status_code == 202
        assert (await settle(app, sid))["status"] == "completed"
        assert all(r["url"] == "https://compatible.test/v1/chat/completions" for r in requests)
        assert len(requests[2]["body"]["messages"]) >= 4
        capabilities = (await client.get("/v1/capabilities")).json()
        assert capabilities["stage_models"]["Planner"]["provider"] == "custom"
        assert capabilities["stage_models"]["Formatter"]["consumption_excluded"] is False
        assert any(e["kind"] == "reasoning_end" for e in app.state.db.events(sid)) is False
        assert Decimal(app.state.db.budget_snapshot(sid, 1, settings)["spent"]) > 0


def test_credential_isolation_and_provider_validation(settings):
    settings.main_provider = "custom"
    assert key_for(settings, "main") == ""
    with pytest.raises(ValidationError):
        TextProvider(protocol="openai_chat", base_url="https://secret:pass@host/v1")
    with pytest.raises(ValidationError):
        TextProvider(
            protocol="openai_chat", base_url="https://host", price_rates_cny={"x": [0, float("nan"), 1]}
        )


@pytest.mark.parametrize("main_key", ["", "synthetic-main-key"])
async def test_search_credentials_never_override_teaching_credentials(settings, main_key):
    settings.deepseek_key = SecretStr("synthetic-legacy-key")
    settings.text_main_key = SecretStr(main_key)
    settings.text_search_key = SecretStr("synthetic-search-key")
    db = Database(settings.db_path)
    db.initialize()
    credentials = []

    def handler(request):
        credentials.append(request.headers["x-api-key"])
        return httpx.Response(200, json=llm_text("synthetic result"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = RoutedClient(settings, db, http)
        for stage in ("search", "expert"):
            await client.complete(
                CallContext(1, "credential-routing", stage), system="s", messages=[{"role": "user", "content": "x"}],
                model=settings.main_model, effort="off", max_tokens=256,
            )
    assert credentials == ["synthetic-search-key", main_key or "synthetic-legacy-key"]


async def test_process_role_is_available_to_planner(settings, monkeypatch):
    from core.configuration import STAGES

    monkeypatch.setattr(STAGES["Planner"], "model_role", "process")
    db = Database(settings.db_path)
    db.initialize()
    captured = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=complete_payload('{"expert":"Euclid"}'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await RoutedClient(settings, db, http).complete(
            CallContext(1, "planner-role", "planner"), system="JSON", messages=[{"role": "user", "content": "x"}],
            model=settings.process_model, effort="off", max_tokens=256,
        )
    assert json.loads(result["content"][0]["text"])["expert"] == "Euclid"
    assert captured[0]["enable_thinking"] is False
    assert db.usage(1)["items"][0]["cost"] == "0"
