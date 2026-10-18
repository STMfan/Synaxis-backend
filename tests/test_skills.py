import base64
import io
import json

import httpx
import pytest
from conftest import llm_text
from PIL import Image
from pydantic import SecretStr

from domain.errors import HarnessError
from llm.contracts import CallContext
from llm.deepseek import DeepSeekClient
from storage.artifacts import ArtifactStore
from storage.database import Database, now
from tools.executor import SkillExecutor


def png_bytes():
    b = io.BytesIO()
    Image.new("RGB", (20, 20), "red").save(b, format="PNG")
    return b.getvalue()


def skill_setup(settings, http):
    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO users(username,password_hash,created_at) VALUES('test','hash',?)", (now(),))
    sid = db.create_session(1, {})
    artifacts = ArtifactStore(settings, db)
    llm = DeepSeekClient(settings, db, http)
    return SkillExecutor(settings, db, llm, http, artifacts), CallContext(1, sid, "expert"), db


async def test_search_uses_native_tool_and_low_effort(settings):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [{"type": "web_search_tool_result", "content": []}],
                "usage": {"input_tokens": 5, "output_tokens": 10},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor, ctx, db = skill_setup(settings, http)
        assert (await executor.execute(ctx, "Search", {"query": "课标"}, ["Search"]))["sources"] == []
        assert requests[0]["tools"][0]["type"] == "web_search_20250305"
        assert requests[0]["output_config"]["effort"] == "low"
        assert db.usage(1)["items"][0]["stage"] == "search"


async def test_assistant_isolated_and_no_recursion(settings):
    requests = []
    replies = [
        {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "AssistantOnWeb", "input": {"task": "recursive"}}
            ]
        },
        llm_text("调查结论"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=replies.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor, ctx, db = skill_setup(settings, http)
        report = await executor.execute(ctx, "AssistantOnWeb", {"task": "调查某细节"}, ["AssistantOnWeb"])
        assert report == {"report": "调查结论"}
        assert len(requests[0]["messages"]) == 1
        assert {t["name"] for t in requests[0]["tools"]} == {"Search", "Syllabus"}
        assert "TOOL_NOT_ALLOWED" in requests[1]["messages"][-1]["content"][0]["content"]
        assert all(c["stage"] == "assistant" for c in db.usage(1)["items"])


async def test_paid_image_permission_billing_and_artifact(settings):
    settings.image_credit = 125
    settings.image_base = "https://image.test/v1"
    settings.image_key = SecretStr("image-test-secret")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(png_bytes()).decode()}],
                "usage": {"input_tokens": 3, "output_tokens": 4},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        executor, ctx, db = skill_setup(settings, http)
        with pytest.raises(HarnessError, match="PAID_IMAGE_APPROVAL_REQUIRED"):
            await executor.execute(ctx, "GPTImage", {"prompt": "图"}, ["GPTImage"])
        assert not requests and db.usage(1)["items"] == []
        result = await executor.execute(ctx, "GPTImage", {"prompt": "图"}, ["GPTImage"], paid_images=True)
        assert result["mime"] == "image/png"
        call = db.usage(1)["items"][0]
        assert call["cost"] == "1.25" and call["currency"] == "CNY"
        assert call["input_tokens"] == 3
        assert str(requests[0].url) == "https://image.test/v1/images/generations"
        assert "image-test-secret" not in str(call) and "response" not in call
        with db.connection(True) as c:
            audit = c.execute("SELECT response FROM calls WHERE id=?", (call["id"],)).fetchone()[0]
        assert "b64_json" not in audit


async def test_successful_image_decode_failure_preserves_cost(settings):
    settings.image_credit = 10
    settings.image_base = "https://image.test/v1"
    settings.image_key = SecretStr("image-test-secret")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"url": "https://evil/file"}]})
        )
    ) as http:
        executor, ctx, db = skill_setup(settings, http)
        with pytest.raises(HarnessError) as error:
            await executor.image(ctx, "图", True)
        assert error.value.code == "IMAGE_BASE64_REQUIRED"
        assert db.usage(1)["items"][0]["cost"] == "0.1"


async def test_python_local_authorized_render_input_and_output(settings, monkeypatch):
    requests = []

    async def runner(settings, code, inputs):
        requests.append({"code": code, "inputs": inputs})
        return {
                "exit_code": 0,
                "stdout": "done",
                "stderr": "",
                "files": [
                    {
                        "name": "output.png",
                        "mime": "image/png",
                        "data": base64.b64encode(png_bytes()).decode(),
                    }
                ],
            }

    monkeypatch.setattr("tools.executor.run_nsjail", runner)

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: pytest.fail("No remote worker request"))) as http:
        executor, ctx, _ = skill_setup(settings, http)
        render = executor.artifacts.write(ctx, "render-output.png", "image/png", png_bytes())
        result = await executor.execute(
            ctx,
            "PythonBox",
            {"code": "print(6)", "input_artifact_ids": [render["artifact_id"]]},
            ["PythonBox"],
        )
        assert result["exit_code"] == 0 and len(result["files"]) == 2
        assert requests[0]["inputs"][render["artifact_id"]] == png_bytes()
        other = executor.artifacts.write(ctx, "image.png", "image/png", png_bytes())
        with pytest.raises(HarnessError, match="ARTIFACT_NOT_FOUND"):
            await executor.execute(
                ctx,
                "PythonBox",
                {"code": "print(1)", "input_artifact_ids": [other["artifact_id"]]},
                ["PythonBox"],
            )
        with pytest.raises(HarnessError, match="TOOL_NOT_ALLOWED"):
            await executor.execute(ctx, "PythonBox", {"code": "print(1)"}, [])


async def test_integrated_sandbox_validates_ast_and_disabled_mode(settings, monkeypatch):
    calls = []

    async def runner(settings, code, inputs):
        calls.append((code, inputs))
        return {"exit_code": 0, "stdout": "6", "stderr": "", "files": []}

    monkeypatch.setattr("tools.executor.run_nsjail", runner)
    async with httpx.AsyncClient() as http:
        executor, ctx, _ = skill_setup(settings, http)
        with pytest.raises(HarnessError):
            await executor.execute(ctx, "PythonBox", {"code": "import os"}, ["PythonBox"])
        assert not calls
        settings.sandbox_enabled = False
        with pytest.raises(HarnessError, match="SANDBOX_UNAVAILABLE"):
            await executor.execute(ctx, "PythonBox", {"code": "print(6)"}, ["PythonBox"])
        assert not calls
