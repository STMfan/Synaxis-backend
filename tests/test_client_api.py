import asyncio
import base64
import json
from datetime import UTC, datetime

from conftest import llm_text, plan_payload, settle, student_image


async def test_idempotent_create_replay_scope_conflict_and_capacity(app_factory):
    async with app_factory([plan_payload(), llm_text("answer")]) as (
        app,
        client,
        requests,
        remaining,
    ):
        body, headers = {"message": "方程", "subject": "math"}, {"Idempotency-Key": "request-001"}
        first, second = await asyncio.gather(
            *[client.post("/v1/sessions", json=body, headers=headers) for _ in range(2)]
        )
        assert first.status_code == second.status_code == 202 and first.json() == second.json()
        sid = first.json()["id"]
        await settle(app, sid)
        app.state.harness.max_pending = 0
        replay = await client.post("/v1/sessions", json=body, headers=headers)
        assert replay.json() == first.json() and replay.headers["Idempotency-Replayed"] == "true"
        assert (await client.post("/v1/sessions", json={"message": "different"}, headers=headers)).json()[
            "code"
        ] == "IDEMPOTENCY_CONFLICT"
        assert (
            await client.post("/v1/sessions", json=body, headers={"Idempotency-Key": "fresh"})
        ).status_code == 503
        assert len(requests) == 2 and not remaining
        assert len((await client.get(f"/v1/sessions/{sid}/messages")).json()["items"]) == 2
        app.state.harness.max_pending = 32
        remaining.extend([plan_payload(), llm_text("bob")])
        bob = (
            await client.post("/v1/auth/token", data={"username": "bob", "password": "strong-test-password"})
        ).json()["access_token"]
        other = await client.post(
            "/v1/sessions", json=body, headers={**headers, "Authorization": "Bearer " + bob}
        )
        assert other.status_code == 202 and other.json()["id"] != sid
        await settle(app, other.json()["id"])


async def test_follow_up_preserves_context_and_public_history(app_factory):
    secret = {
        "content": [
            {"type": "thinking", "thinking": "private-provider-thinking", "signature": "s"},
            {"type": "text", "text": "first"},
        ],
        "usage": {"input_tokens": 2, "output_tokens": 4},
    }
    async with app_factory([plan_payload(), secret, llm_text("another method")]) as (
        app,
        client,
        requests,
        _,
    ):
        original = await client.post("/v1/sessions", json={"message": "初始问题", "subject": "math"})
        sid = original.json()["id"]
        await settle(app, sid)
        body, headers = {"message": "第二步没懂"}, {"Idempotency-Key": "follow-up-1"}
        response = await client.post(f"/v1/sessions/{sid}/messages", json=body, headers=headers)
        assert response.status_code == 202
        await settle(app, sid)
        assert (
            await client.post(f"/v1/sessions/{sid}/messages", json=body, headers=headers)
        ).json() == response.json()
        assert len(requests) == 3
        assert requests[2]["body"]["messages"][-1]["content"][0]["text"] == body["message"]
        assert "初始问题" in requests[2]["body"]["messages"][0]["content"][0]["text"]
        messages = (await client.get(f"/v1/sessions/{sid}/messages?limit=2")).json()
        assert [m["role"] for m in messages["items"]] == ["student", "assistant"]
        second = (await client.get(f"/v1/sessions/{sid}/messages?after={messages['next_after']}")).json()
        assert [m["kind"] for m in second["items"]] == ["follow_up", "presentation"]
        assert "private-provider-thinking" not in json.dumps([messages, second])
        usage = await client.get("/v1/usage")
        assert "request" not in usage.json()["items"][0] and "private-provider-thinking" not in usage.text
        events = (await client.get(f"/v1/sessions/{sid}/events/history")).json()
        assert "running" in [x["event"] for x in events["items"]]
        assert sum(x["event"] == "message" for x in events["items"]) == 4
        assert "private-provider-thinking" in json.dumps(events)
        assert any(x["event"] == "reasoning_delta" for x in events["items"])
        detail = (await client.get(f"/v1/sessions/{sid}")).json()
        assert detail["original_message"] == "初始问题" and detail["created_at"] and detail["updated_at"]


async def test_waiting_requires_answer_and_answer_retries_are_idempotent(app_factory):
    question = {"type": "question", "id": "q1", "prompt": "会吗？", "options": []}
    tool = {
        "content": [
            {
                "type": "tool_use",
                "id": "ask1",
                "name": "AskStudent",
                "input": {k: v for k, v in question.items() if k != "type"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    async with app_factory([plan_payload(), tool, llm_text("继续")]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "题目"})).json()["id"]
        await settle(app, sid)
        assert (await client.post(f"/v1/sessions/{sid}/messages", json={"message": "另一个问题"})).json()[
            "code"
        ] == "ANSWER_REQUIRED"
        body, headers = {"question_id": "q1", "answer": "会"}, {"Idempotency-Key": "answer-1"}
        response = await client.post(f"/v1/sessions/{sid}/answers", json=body, headers=headers)
        await settle(app, sid)
        assert (
            await client.post(f"/v1/sessions/{sid}/answers", json=body, headers=headers)
        ).json() == response.json()
        assert len(requests) == 3
        records = (await client.get(f"/v1/sessions/{sid}/messages")).json()["items"]
        assert [x["kind"] for x in records] == ["question", "presentation", "answer", "presentation"]
        assert records[2]["text"] == "会" and records[2]["question_id"] == "q1"


async def test_directory_filters_paging_management_and_deleted_receipt(app_factory):
    async with app_factory([item for _ in range(3) for item in (plan_payload(), llm_text("answer"))]) as (
        app,
        client,
        _,
        _,
    ):
        ids = []
        for index in range(3):
            sid = (
                await client.post(
                    "/v1/sessions",
                    json={"message": f"数学方程{index}"},
                    headers={"Idempotency-Key": f"create-{index}"},
                )
            ).json()["id"]
            await settle(app, sid)
            ids.append(sid)
        patched = await client.patch(f"/v1/sessions/{ids[0]}", json={"title": "收藏的方程", "pinned": True})
        assert patched.status_code == 200
        page = (await client.get("/v1/sessions?limit=1&subject=math&status=completed&q=方程")).json()
        assert page["items"][0]["id"] == ids[0]
        next_page = (
            await client.get(
                "/v1/sessions",
                params={
                    "limit": 2,
                    "subject": "math",
                    "status": "completed",
                    "q": "方程",
                    "cursor": page["next_cursor"],
                },
            )
        ).json()
        assert len(next_page["items"]) == 2 and ids[0] not in [x["id"] for x in next_page["items"]]
        assert (
            await client.get("/v1/sessions", params={"cursor": page["next_cursor"], "q": "different"})
        ).status_code == 400
        await client.patch(f"/v1/sessions/{ids[0]}", json={"archived": True})
        assert len((await client.get("/v1/sessions")).json()["items"]) == 2
        assert (await client.get("/v1/sessions?archived=only")).json()["items"][0]["id"] == ids[0]
        assert (await client.post(f"/v1/sessions/{ids[0]}/messages", json={"message": "继续"})).json()[
            "code"
        ] == "SESSION_ARCHIVED"
        file = (await client.get(f"/v1/sessions/{ids[0]}")).json()["presentations"][0]["blocks"][-1][
            "artifact_id"
        ]
        assert (await client.delete(f"/v1/sessions/{ids[0]}")).status_code == 204
        assert (await client.delete(f"/v1/sessions/{ids[0]}")).status_code == 204
        for suffix in ("", "/messages", "/events", "/events/history", "/budget"):
            assert (await client.get(f"/v1/sessions/{ids[0]}{suffix}")).status_code == 404
        assert (await client.get(f"/v1/artifacts/{file}")).status_code == 404
        assert (
            await client.post(
                "/v1/sessions", json={"message": "数学方程0"}, headers={"Idempotency-Key": "create-0"}
            )
        ).status_code == 410
        assert len((await client.get("/v1/usage")).json()["items"]) == 6


async def test_image_history_ownership_and_poiesis_followup(app_factory):
    image = student_image()
    async with app_factory([plan_payload(), llm_text("answer")]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "图片题目", "images": [image]})).json()[
            "id"
        ]
        await settle(app, sid)
        record = (await client.get(f"/v1/sessions/{sid}/messages")).json()["items"][0]
        assert record["images"][0]["bytes"] > 0 and "data" not in record["images"][0]
        image_url = record["images"][0]["url"]
        assert (await client.get(image_url)).content == base64.b64decode(image["data"])
        bob = (
            await client.post("/v1/auth/token", data={"username": "bob", "password": "strong-test-password"})
        ).json()["access_token"]
        for url in (
            image_url,
            f"/v1/sessions/{sid}/messages",
            f"/v1/sessions/{sid}/budget",
            f"/v1/sessions/{sid}/events/history",
        ):
            assert (await client.get(url, headers={"Authorization": "Bearer " + bob})).status_code == 404
        assert (await client.get("/v1/sessions", headers={"Authorization": "Bearer " + bob})).json()[
            "items"
        ] == []
        with app.state.db.connection() as c:
            state = app.state.db.raw_session(c, sid)["state"]
            state["plan"]["expert"], state["plan"]["subject"] = "Poiesis", "chinese"
            c.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(state), sid))
        assert (
            await client.post(f"/v1/sessions/{sid}/messages", json={"message": "看图片", "images": [image]})
        ).status_code == 400


async def test_preferences_defaults_and_explicit_overrides(app_factory):
    async with app_factory([plan_payload(), llm_text("answer")]) as (app, client, requests, _):
        prefs = {"grade": "高一", "province": "浙江", "textbook": "人教A版", "subject": "math"}
        assert (await client.put("/v1/me/preferences", json=prefs)).json() == prefs
        profile = (await client.get("/v1/me")).json()
        assert profile["preferences"] == prefs and "password_hash" not in profile
        sid = (await client.post("/v1/sessions", json={"message": "教材问题", "grade": "高二"})).json()["id"]
        await settle(app, sid)
        detail = (await client.get(f"/v1/sessions/{sid}")).json()
        assert detail["grade"] == "高二" and detail["province"] == "浙江" and detail["subject"] == "math"
        assert "人教A版" in requests[0]["body"]["messages"][0]["content"][0]["text"]
        assert (await client.put("/v1/me/preferences", json={"allow_paid_images": True})).status_code == 422


async def test_refresh_rotation_replay_revokes_all_tokens(app_factory):
    async with app_factory() as (_, client, _, _):
        login = (
            await client.post(
                "/v1/auth/token", data={"username": "alice", "password": "strong-test-password"}
            )
        ).json()
        rotated = await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})
        assert rotated.status_code == 200
        newer = rotated.json()
        assert newer["refresh_token"] != login["refresh_token"]
        assert (await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})).json()[
            "code"
        ] == "REFRESH_REUSED"
        assert (
            await client.get("/v1/me", headers={"Authorization": "Bearer " + newer["access_token"]})
        ).status_code == 401
        assert (
            await client.post("/v1/auth/refresh", json={"refresh_token": newer["refresh_token"]})
        ).status_code == 401


async def test_password_change_and_logout_revoke_auth(app_factory):
    async with app_factory() as (_, client, _, _):
        login = (
            await client.post(
                "/v1/auth/token", data={"username": "alice", "password": "strong-test-password"}
            )
        ).json()
        assert (
            await client.post(
                "/v1/me/password", json={"current_password": "wrong", "new_password": "new-secure-password"}
            )
        ).status_code == 401
        assert (
            await client.post(
                "/v1/me/password",
                json={"current_password": "strong-test-password", "new_password": "new-secure-password"},
            )
        ).status_code == 204
        assert (await client.get("/v1/me")).status_code == 401
        assert (
            await client.post("/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})
        ).status_code == 401
        new = (
            await client.post("/v1/auth/token", data={"username": "alice", "password": "new-secure-password"})
        ).json()
        headers = {"Authorization": "Bearer " + new["access_token"]}
        assert (await client.post("/v1/auth/logout", headers=headers)).status_code == 204
        assert (await client.get("/v1/me", headers=headers)).status_code == 401


async def test_cors_and_capability_contracts(app_factory):
    async with app_factory() as (_, client, _, _):
        headers = {
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,idempotency-key,last-event-id",
        }
        allowed = await client.options("/v1/sessions", headers=headers)
        assert (
            allowed.status_code == 200 and allowed.headers["Access-Control-Allow-Origin"] == headers["Origin"]
        )
        assert (
            await client.options("/v1/sessions", headers={**headers, "Origin": "https://untrusted.test"})
        ).status_code == 400
        cap = (await client.get("/v1/capabilities")).json()
        assert cap["budget_enforced"] and cap["session_budget_cny"] == "5.0"
        assert cap["stage_efforts"]["Planner"] == cap["stage_efforts"]["Formatter"] == "off"
        assert cap["stage_max_output_tokens"]["Planner"] == 393216
        assert next(e for e in cap["experts"] if e["name"] == "Poiesis")["images"] is False
        assert next(s for s in cap["skills"] if s["name"] == "Syllabus")["availability"] == "unavailable"
        schema = (await client.get("/openapi.json")).json()
        assert (
            "ExpertsResponse" in schema["components"]["schemas"]
            and "MessageEvent" in schema["components"]["schemas"]
        )


async def test_usage_summary_decimal_unknown_currency_separation(app_factory):
    async with app_factory() as (app, client, _, _):
        for currency, cost in (("CNY", "0.1"), ("CNY", "0.2"), ("CNY", None), ("USD", "0.5")):
            call = app.state.db.begin_call(
                1, "session1", "expert", "m", {"private": "system-prompt"}, currency=currency
            )
            app.state.db.finish_call(call, response={"thinking": "private"}, cost=cost)
        today = datetime.now(UTC).date().isoformat()
        page = (
            await client.get(
                "/v1/usage/summary",
                params={"group_by": "session", "date_from": today, "date_to": today, "limit": 1},
            )
        ).json()
        more = (
            await client.get(
                "/v1/usage/summary",
                params={
                    "group_by": "session",
                    "date_from": today,
                    "date_to": today,
                    "cursor": page["next_cursor"],
                },
            )
        ).json()
        groups = {x["currency"]: x for x in page["items"] + more["items"]}
        assert groups["CNY"]["estimated_cost"] == "0.3" and groups["CNY"]["unknown_cost_calls"] == 1
        assert groups["USD"]["estimated_cost"] == "0.5"
        assert "system-prompt" not in (await client.get("/v1/usage")).text
        assert (
            await client.get("/v1/usage/summary?date_from=2026-01-02&date_to=2026-01-01")
        ).status_code == 422


async def test_delete_running_and_no_followup_race(app_factory):
    entered = asyncio.Event()

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    async with app_factory([blocked]) as (app, client, requests, _):
        sid = (await client.post("/v1/sessions", json={"message": "题目"})).json()["id"]
        await entered.wait()
        busy = await client.post(f"/v1/sessions/{sid}/messages", json={"message": "继续"})
        assert busy.status_code == 409 and busy.json()["code"] == "SESSION_BUSY"
        assert (await client.delete(f"/v1/sessions/{sid}")).status_code == 204
        assert app.state.db.session(sid) is None and len(requests) == 1


async def test_cancel_queued_persists_terminal_state(app_factory):
    async with app_factory() as (app, client, requests, _):
        await app.state.harness.gate.acquire()
        # The configured gate has four slots; occupy all before submitting.
        for _ in range(3):
            await app.state.harness.gate.acquire()
        sid = (await client.post("/v1/sessions", json={"message": "题目"})).json()["id"]
        response = await client.post(f"/v1/sessions/{sid}/cancel")
        assert response.json()["status"] == "cancelled" and not requests
        for _ in range(4):
            app.state.harness.gate.release()


async def test_failed_tool_protocol_can_continue_without_replanning(app_factory):
    broken = {
        "content": [{"type": "tool_use", "name": "Syllabus", "input": {}}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    async with app_factory([plan_payload(), broken, llm_text("恢复讲解")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "原始问题"})).json()["id"]
        assert (await settle(app, sid))["status"] == "failed"
        response = await client.post(f"/v1/sessions/{sid}/messages", json={"message": "请继续"})
        assert response.status_code == 202
        assert (await settle(app, sid))["status"] == "completed"
        history = requests[2]["body"]["messages"]
        assert not any(b.get("type") == "tool_use" for m in history for b in m["content"])
        assert len(requests) == 3


async def test_invalid_images_rejected_before_paid_dispatch(app_factory):
    async with app_factory() as (_, client, requests, _):
        invalid = {"media_type": "image/png", "data": base64.b64encode(b"not-a-picture").decode()}
        assert (
            await client.post("/v1/sessions", json={"message": "图片", "images": [invalid]})
        ).status_code == 422
        wrong_mime = {**student_image(), "media_type": "image/jpeg"}
        assert (
            await client.post("/v1/sessions", json={"message": "图片", "images": [wrong_mime]})
        ).status_code == 422
        assert (await client.post("/v1/sessions", json={"message": "   "})).status_code == 422
        assert not requests
        catalog = (await client.get("/v1/error-codes")).json()["items"]
        assert next(e for e in catalog if e["code"] == "BUDGET_EXCEEDED")["recovery"] == "stop_paid_calls"
