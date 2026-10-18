"""Voucher exclusion, Off, bounded contexts and no paid fallback/retry."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from conftest import formatted, llm_text, plan_payload, settle

from core.configuration import STAGES
from core.presentation import validate_presentation
from domain.errors import HarnessError
from domain.schemas import Presentation
from llm.contracts import CallContext
from llm.siliconflow import SiliconFlowClient
from storage.database import Database


def reply(**changes):
    value = {
        "choices": [
            {
                "message": {"content": '{"blocks":[{"type":"markdown","text":"答案42"}]}'},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 40},
        },
    }
    value.update(changes)
    return value


def test_server_question_append_does_not_accept_changed_or_invented_question():
    question = {"type": "question", "id": "q1", "prompt": "你会吗？", "options": ["会"]}
    p = Presentation.model_validate({"blocks": [{"type": "markdown", "text": "讲解"}]})
    assert validate_presentation(p, [], question, append_question=True).blocks[-1].model_dump() == question
    for pending in (question, None):
        altered = {**question, "prompt": "换一道题？"}
        bad = Presentation.model_validate({"blocks": [altered]})
        with pytest.raises(ValueError, match="Changed or invented"):
            validate_presentation(bad, [], pending, append_question=True)


async def test_excluded_formatter_runs_at_exhausted_budget_and_audits_tokens(settings):
    db = Database(settings.db_path)
    db.initialize()
    call = db.begin_call(
        1, "s", "expert", settings.main_model, {}, currency="CNY", reservation=Decimal(5), settings=settings
    )
    db.finish_call(call, cost=Decimal(5))
    requests = []

    def handler(r):
        requests.append(json.loads(r.content))
        return httpx.Response(200, json=reply())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        value = await SiliconFlowClient(settings, db, http).complete(
            CallContext(1, "s", "formatter"),
            system="JSON",
            messages=[{"role": "user", "content": "x"}],
            model=settings.process_model,
            effort="off",
            max_tokens=393216,
        )
    body = requests[0]
    assert body["enable_thinking"] is False and body["response_format"] == {"type": "json_object"}

    assert 8192 < body["max_tokens"] <= settings.providers[settings.process_provider].output_limits[settings.process_model]
    assert body["max_tokens"] < settings.providers[settings.process_provider].context_limits[settings.process_model]
    assert value["usage"] == {"input_tokens": 60, "output_tokens": 20, "cache_read_input_tokens": 40}
    row = db.usage(1)["items"][0]
    assert row["cost"] == "0" and row["pricing_basis"].startswith("excluded:siliconflow:")
    assert "request" not in row and "response" not in row
    snapshot = db.budget_snapshot("s", 1, settings)
    assert Decimal(snapshot["spent"]) == 5 and Decimal(snapshot["uncertain"]) == 0
    # Reconstructing after budget metadata loss still respects excluded calls.
    with db.connection(True) as c:
        c.execute("DELETE FROM budget_reservations")
        c.execute("DELETE FROM session_budgets")
    assert Decimal(db.budget_snapshot("s", 1, settings)["spent"]) == 5


@pytest.mark.parametrize(
    "payload",
    [
        {},
        reply(choices=[]),
        reply(choices=[{"message": {"content": ""}, "finish_reason": "stop"}]),
        reply(usage={"prompt_tokens": -1, "completion_tokens": 20}),
        reply(usage={"prompt_tokens": True, "completion_tokens": 20}),
        reply(
            usage={"prompt_tokens": 1, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 2}}
        ),
        reply(
            choices=[{"message": {"content": "x", "reasoning_content": "private"}, "finish_reason": "stop"}]
        ),
    ],
)
async def test_invalid_processor_response_is_excluded_but_marked_failed(settings, payload):
    db = Database(settings.db_path)
    db.initialize()
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(HarnessError, match="PROCESSOR_INVALID_RESPONSE"):
            await SiliconFlowClient(settings, db, http).complete(
                CallContext(1, "s", "formatter"),
                system="JSON",
                messages=[{"role": "user", "content": "x"}],
                model=settings.process_model,
                effort="off",
            )
    assert len(seen) == 1
    record = db.usage(1)["items"][0]
    assert record["status"] == "failed" and record["cost"] == "0"
    assert Decimal(db.budget_snapshot("s", 1, settings)["uncertain"]) == 0


@pytest.mark.parametrize("cancel", [False, True])
async def test_processor_transport_failure_does_not_retry_or_charge(settings, cancel):
    db = Database(settings.db_path)
    db.initialize()
    seen = []

    async def handler(request):
        seen.append(request)
        if cancel:
            raise asyncio.CancelledError
        raise httpx.ReadTimeout("network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(asyncio.CancelledError if cancel else HarnessError):
            await SiliconFlowClient(settings, db, http).complete(
                CallContext(1, "s", "formatter"),
                system="JSON",
                messages=[{"role": "user", "content": "x"}],
                model=settings.process_model,
                effort="off",
            )
    record = db.usage(1)["items"][0]
    assert len(seen) == 1 and record["status"] == ("cancelled" if cancel else "failed")
    assert record["cost"] == "0"


async def test_invalid_processor_scope_limits_and_context_fail_before_transport(settings):
    db = Database(settings.db_path)
    db.initialize()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected request"))
    ) as http:
        client = SiliconFlowClient(settings, db, http)
        kwargs = dict(
            system="JSON",
            messages=[{"role": "user", "content": "x"}],
            model=settings.process_model,
            effort="off",
        )
        for options, code in [
            ({"effort": "low"}, "PROCESSOR_INVALID_REQUEST"),
            ({"model": "unknown"}, "PROCESSOR_LIMITS_UNKNOWN"),
            ({"messages": [{"role": "user", "content": [{"type": "image"}]}]}, "PROCESSOR_INVALID_REQUEST"),
            ({"messages": [{"role": "user", "content": "x" * 200000}]}, "PROCESSOR_CONTEXT_TOO_LARGE"),
        ]:
            with pytest.raises(HarnessError, match=code):
                await client.complete(CallContext(1, "s", "formatter"), **{**kwargs, **options})
        with pytest.raises(HarnessError, match="PROCESSOR_INVALID_REQUEST"):
            await client.complete(CallContext(1, "s", "expert"), **kwargs)
    assert not db.usage(1)["items"]


async def test_formatter_role_uses_siliconflow_ignoring_legacy_stage_provider(app_factory, monkeypatch):
    monkeypatch.setitem(
        STAGES, "Formatter", STAGES["Formatter"].model_copy(update={"provider": "deepseek"})
    )
    async with app_factory([plan_payload(), llm_text("<b>答案42</b>"), formatted("答案42")]) as (
        app,
        client,
        requests,
        remaining,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "解释"})).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        assert all("api.deepseek.com" in r["url"] for r in requests[:2])
        assert (
            "api.siliconflow.cn" in requests[-1]["url"] and requests[-1]["body"]["enable_thinking"] is False
        )
        assert app.state.db.usage(1)["items"][0]["cost"] == "0"
        capabilities = (await client.get("/v1/capabilities")).json()
        info = capabilities["stage_models"]["Formatter"]
        assert info["provider"] == "siliconflow" and info["consumption_excluded"]

        settings = app.state.settings
        assert info["context_limit"] == settings.providers[settings.process_provider].context_limits[info["model"]]
        assert info["effort"] == "off"
        assert capabilities["stage_models"]["Planner"]["provider"] == "deepseek"
        assert not capabilities["stage_models"]["Planner"]["consumption_excluded"]
        assert len(requests) == 3 and not remaining
