import asyncio
import json

import jwt
import pytest
from conftest import llm_text, plan_payload, settle, student_image


async def test_full_tutoring_sse_usage_artifact(app_factory):
    async with app_factory([plan_payload(), llm_text("分解为 (x−2)(x−3)=0，x=2 或 x=3")]) as (
        app,
        client,
        requests,
        remaining,
    ):
        response = await client.post("/v1/sessions", json={"message": "解 x²−5x+6=0", "subject": "math"})
        assert response.status_code == 202
        sid = response.json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        result = (await client.get(f"/v1/sessions/{sid}")).json()
        assert result["expert"] == "Euclid"
        assert result["presentations"][0]["blocks"][0]["text"] == "分解为 (x−2)(x−3)=0，x=2 或 x=3"
        stream = await client.get(f"/v1/sessions/{sid}/events")
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert "event: presentation" in stream.text and "event: completed" in stream.text
        ids = [int(line[4:]) for line in stream.text.splitlines() if line.startswith("id: ")]
        assert (
            await client.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": str(ids[-1])})
        ).text == ""
        usage = (await client.get("/v1/usage")).json()["items"]
        assert [call["stage"] for call in usage] == ["expert", "planner"]
        assert all(call["cost"] is not None and call["status"] == "success" for call in usage)
        assert "unit-test-key" not in json.dumps(usage)
        file = result["presentations"][0]["blocks"][-1]["artifact_id"]
        downloaded = await client.get(f"/v1/artifacts/{file}")
        assert downloaded.status_code == 200 and "分解" in downloaded.text
        assert "attachment" in downloaded.headers["content-disposition"]
        assert not remaining and len(requests) == 2
        assert requests[1]["body"]["stream"] is True
        assert "event: output_delta" in stream.text


async def test_question_resume_thinking_and_duplicate(app_factory):
    question = {"type": "question", "id": "q1", "prompt": "你会因式分解吗？", "options": ["会", "不会"]}
    ask = {
        "content": [
            {"type": "thinking", "thinking": "private", "signature": "test"},
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "AskStudent",
                "input": {k: v for k, v in question.items() if k != "type"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    async with app_factory([plan_payload(), ask, llm_text("用因式分解求根")]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "解方程"})).json()["id"]
        row = await settle(app, sid)
        assert row["status"] == "waiting" and row["state"]["question"] == question
        assert "private" not in (await client.get(f"/v1/sessions/{sid}")).text
        assert (
            await client.post(f"/v1/sessions/{sid}/answers", json={"question_id": "wrong", "answer": "会"})
        ).status_code == 409
        answer = {"question_id": "q1", "answer": "会"}
        assert (await client.post(f"/v1/sessions/{sid}/answers", json=answer)).status_code == 202
        await settle(app, sid)
        assert (await client.post(f"/v1/sessions/{sid}/answers", json=answer)).status_code == 409
        assert app.state.db.session(sid)["status"] == "completed"
        history = requests[2]["body"]["messages"]
        assert history[1]["content"][0]["thinking"] == "private"
        assert history[2]["content"][0]["tool_use_id"] == "tool-1"
        assert "会" in history[-1]["content"][0]["text"] and len(requests) == 3


async def test_ownership(app_factory):
    async with app_factory([plan_payload(), llm_text("解答")]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "解题"})).json()["id"]
        row = await settle(app, sid)
        artifact = row["state"]["presentations"][0]["blocks"][-1]["artifact_id"]
        token = (
            await client.post("/v1/auth/token", data={"username": "bob", "password": "strong-test-password"})
        ).json()["access_token"]
        client.headers["Authorization"] = "Bearer " + token
        for url in (f"/v1/sessions/{sid}", f"/v1/sessions/{sid}/events", f"/v1/artifacts/{artifact}"):
            assert (await client.get(url)).status_code == 404
        assert (await client.post(f"/v1/sessions/{sid}/cancel")).status_code == 404
        assert (await client.get("/v1/usage")).json()["items"] == []


async def test_auth_revoke_disable_missing_claims(app_factory):
    async with app_factory() as (app, client, _, _):
        assert (await client.get("/v1/experts")).status_code == 200
        with app.state.db.connection() as conn:
            conn.execute("UPDATE users SET token_version=token_version+1 WHERE username='alice'")
        assert (await client.get("/v1/experts")).status_code == 401
        with app.state.db.connection() as conn:
            conn.execute("UPDATE users SET active=0 WHERE username='alice'")
        assert (
            await client.post(
                "/v1/auth/token", data={"username": "alice", "password": "strong-test-password"}
            )
        ).status_code == 401
        client.headers["Authorization"] = "Bearer " + jwt.encode(
            {"sub": "1", "ver": 0}, app.state.secret, algorithm="HS256"
        )
        assert (await client.get("/v1/usage")).status_code == 401
        assert (await client.post("/v1/auth/register", json={})).status_code == 404
        client.headers.pop("Authorization")
        assert (await client.get("/v1/usage")).status_code == 401


async def test_rate_limit_validation_redaction(app_factory):
    async with app_factory() as (_, client, _, _):
        for _ in range(9):
            assert (
                await client.post("/v1/auth/token", data={"username": "unknown", "password": "secret"})
            ).status_code == 401
        assert (
            await client.post("/v1/auth/token", data={"username": "unknown", "password": "secret"})
        ).status_code == 429
        bad = await client.post(
            "/v1/sessions",
            json={"message": "x", "images": [{"media_type": "image/png", "data": "my-private-data"}]},
        )
        assert bad.status_code == 422 and "my-private-data" not in bad.text


async def test_docs_and_pagination(app_factory):
    async with app_factory() as (app, client, _, _):
        zh = (await client.get("/openapi.json?lang=zh")).json()
        en = (await client.get("/openapi.json?lang=en")).json()
        assert zh["paths"]["/v1/sessions"]["post"]["summary"] == "开始教学会话"
        assert en["paths"]["/v1/sessions"]["post"]["summary"] == "Start tutoring session"
        assert (
            en["components"]["securitySchemes"]["OAuth2PasswordBearer"]["flows"]["password"]["tokenUrl"]
            == "/v1/auth/token"
        )
        assert "Presentation" in en["components"]["schemas"]
        assert (
            "text/event-stream"
            in en["paths"]["/v1/sessions/{session_id}/events"]["get"]["responses"]["200"]["content"]
        )
        assert "lang=en" in (await client.get("/docs")).text
        assert (await client.get("/docs?lang=fr")).status_code == 422
        for _ in range(5):
            call_id = app.state.db.begin_call(1, "s1", "expert", "m", {"input": "q"})
            app.state.db.finish_call(call_id, cost="0.01")
        page = (await client.get("/v1/usage?limit=2&session_id=s1")).json()
        assert len(page["items"]) == 2 and page["next_before"] is not None
        next_page = (await client.get(f"/v1/usage?limit=2&before={page['next_before']}")).json()
        assert max(x["id"] for x in next_page["items"]) < min(x["id"] for x in page["items"])


async def test_cancel_records_cancelled(app_factory):
    entered = asyncio.Event()

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    async with app_factory([blocked]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "test"})).json()["id"]
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert (await client.post(f"/v1/sessions/{sid}/cancel")).json()["status"] == "cancelled"
        assert (await client.get("/v1/usage")).json()["items"][0]["status"] == "cancelled"


async def test_poiesis_model_and_images(app_factory, settings):
    async with app_factory([plan_payload("Poiesis", "chinese"), llm_text("赏析")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "赏析诗歌", "subject": "chinese"})).json()[
            "id"
        ]
        await settle(app, sid)
        assert requests[1]["body"]["model"] == settings.literature_model
        assert requests[1]["body"]["thinking"] == {"type": "disabled"}
        assert "output_config" not in requests[1]["body"]
        assert (
            await client.post(
                "/v1/sessions",
                json={
                    "message": "赏析",
                    "subject": "chinese",
                    "images": [student_image()],
                },
            )
        ).status_code == 400


@pytest.mark.parametrize(
    "bad",
    ["not JSON", json.dumps({"subject": "english", "expert": "Euclid", "task": "x", "problem_type": "x"})],
)
async def test_invalid_planner_no_retry(app_factory, bad):
    async with app_factory([llm_text(bad)]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        row = await settle(app, sid)
        assert row["status"] == "failed" and row["state"]["error"] == "PLANNER_INVALID_OUTPUT"
        assert len(requests) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "bad JSON",
        json.dumps({"blocks": [{"type": "html", "source": "<script>alert(1)</script>"}]}),
        json.dumps({"blocks": [{"type": "localfile", "artifact_id": "a" * 32, "label": "secret"}]}),
        json.dumps({"blocks": [{"type": "question", "id": "invented", "prompt": "x"}]}),
    ],
)
async def test_formatter_fallback(app_factory, bad):
    async with app_factory([plan_payload(), llm_text("<b>可核验的解答</b>"), llm_text(bad)]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        row = await settle(app, sid)
        assert row["status"] == "completed"
        assert "<b>可核验的解答</b>" in row["state"]["presentations"][0]["blocks"][0]["text"]
        assert row["state"]["presentations"][0]["blocks"][0]["text"].startswith("```text")
        assert len(requests) == 3
        assert any(e["kind"] == "warning" for e in app.state.db.events(sid))


async def test_tool_round_public_output_and_history(app_factory):
    tools = {
        "content": [
            {"type": "thinking", "thinking": "private", "signature": "test"},
            {"type": "text", "text": "先查询课程标准"},
            {
                "type": "tool_use",
                "id": "t1",
                "name": "Syllabus",
                "input": {"subject": "math", "query": "函数"},
            },
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    async with app_factory([plan_payload(), tools, llm_text("课标未建库，以下基于题目")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        row = await settle(app, sid)
        assert len(row["state"]["presentations"]) == 2
        assert requests[2]["body"]["messages"][1]["content"][0]["thinking"] == "private"
        assert "SYLLABUS_NOT_INDEXED" in requests[2]["body"]["messages"][2]["content"][0]["content"]


async def test_timeout_and_step_limit(app_factory, settings):
    settings.session_timeout = 0.05

    async def blocked(request):
        await asyncio.sleep(1)

    async with app_factory([blocked]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        assert (await settle(app, sid))["state"]["error"] == "SESSION_TIMEOUT"
