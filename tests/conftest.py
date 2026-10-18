import base64
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr

# Prompt defaults load at import time. A developer's alternate/private config
# must not change the contract fixtures before the per-test isolation can run.
with patch.dict(os.environ, {"SYNAXIS_CONFIG_DIR": str(Path(__file__).resolve().parents[1] / "config")}, clear=True):
    from core.settings import Settings

    # api.app constructs its conventional ASGI app during import. Disable the
    # dotenv source before that constructor runs, not only in later fixtures.
    with patch.dict(Settings.model_config, {"env_file": None}):
        from api.app import create_app
        from auth.security import hash_password
        from storage.database import Database, now


def settings_environment_names():
    names = {"SYNAXIS_CONFIG_DIR", "SYNAXIS_ENV_FILE", "SYNAXIS_MANAGED_WORKER"}
    for name, field in Settings.model_fields.items():
        names.add(name)
        alias = field.validation_alias
        if isinstance(alias, str):
            names.add(alias)
        elif alias is not None:
            names.update(value for value in alias.choices if isinstance(value, str))
    return {name.casefold() for name in names}


@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch):
    """Use repository JSON defaults and synthetic credentials, never local .env."""
    names = settings_environment_names()
    for name in tuple(os.environ):
        if name.casefold() in names:
            monkeypatch.delenv(name)
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def block_live_http(request, monkeypatch):
    """An omitted MockTransport must fail before any network or paid call."""
    if request.node.get_closest_marker("integration") or request.node.get_closest_marker("syllabus_integration"):
        return

    def blocked(*args, **kwargs):
        raise AssertionError("Unit tests require MockTransport or ASGITransport; live HTTP is disabled")

    async def blocked_async(*args, **kwargs):
        blocked()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


def student_image():
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(stream, format="PNG")
    return {"media_type": "image/png", "data": base64.b64encode(stream.getvalue()).decode()}


def llm_text(text, **extra):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 30},
        **extra,
    }


def plan_payload(expert="Euclid", subject="math", skills=None):
    return llm_text(
        json.dumps(
            {
                "subject": subject,
                "expert": expert,
                "task": "解决并讲解题目",
                "problem_type": "解题",
                "skills": skills or [],
            },
            ensure_ascii=False,
        )
    )


def formatted(text="x=2 或 x=3", question=None):
    blocks = [{"type": "markdown", "text": text}]
    if question:
        blocks.append(question)
    return llm_text(json.dumps({"blocks": blocks}, ensure_ascii=False))


def stream_frames(payload):
    initial = {k: v for k, v in payload.items() if k not in ("content", "stop_reason")}
    initial.update(content=[], stop_reason=None, usage={**payload.get("usage", {}), "output_tokens": 0})
    events = [{"type": "message_start", "message": initial}]
    for index, block in enumerate(payload.get("content", [])):
        start = dict(block)
        kind = block["type"]
        if kind == "text":
            start["text"] = ""
        elif kind == "thinking":
            start["thinking"] = ""
            start.pop("signature", None)
        elif kind in ("tool_use", "server_tool_use"):
            start["input"] = {}
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        if kind in ("text", "thinking"):
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": kind + "_delta", kind: block[kind]},
                }
            )
            if "signature" in block:
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": block["signature"]},
                    }
                )
        elif kind in ("tool_use", "server_tool_use"):
            events.append(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(block.get("input", {}))},
                }
            )
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": payload.get("stop_reason", "end_turn")},
            "usage": payload.get("usage", {}),
        }
    )
    events.append({"type": "message_stop"})
    return [f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n".encode() for e in events]


def stream_response(payload):
    return httpx.Response(
        200, content=b"".join(stream_frames(payload)), headers={"content-type": "text/event-stream"}
    )


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        deepseek_key=SecretStr("unit-test-key"),
        siliconflow_key=SecretStr("unit-test-key"),
        jwt_secret=SecretStr("test-jwt-secret-at-least-thirty-two-characters"),
        syllabus_data_dir=tmp_path / "syllabus/data",
        syllabus_model_cache=tmp_path / "syllabus/models",
        request_timeout=2,
        session_timeout=5,
        render_enabled=False,
        pricing_mode="peak",
    )


@pytest.fixture
def app_factory(settings):
    @asynccontextmanager
    async def factory(responses=None):
        responses, requests = list(responses or []), []

        async def handler(request):
            requests.append(
                {
                    "url": str(request.url),
                    "headers": dict(request.headers),
                    "body": json.loads(request.content),
                }
            )
            if not responses:
                raise AssertionError("Unexpected provider request")
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item = await item(request)
            if isinstance(item, httpx.Response):
                return item
            if request.url.path.endswith("/chat/completions"):
                usage = item.get("usage", {})
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "content": "\n".join(
                                        b.get("text", "") for b in item["content"] if b.get("type") == "text"
                                    )
                                },
                                "finish_reason": "length"
                                if item.get("stop_reason") == "max_tokens"
                                else "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": usage.get("input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0),
                            "completion_tokens": usage.get("output_tokens", 0),
                            "prompt_tokens_details": {
                                "cached_tokens": usage.get("cache_read_input_tokens", 0)
                            },
                        },
                    },
                )
            return (
                stream_response(item)
                if requests[-1]["body"].get("stream")
                else httpx.Response(200, json=item)
            )

        db = Database(settings.db_path)
        db.initialize()
        encoded = hash_password("strong-test-password")
        with db.connection() as conn:
            for username in ("alice", "bob"):
                conn.execute(
                    "INSERT OR IGNORE INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                    (username, encoded, now()),
                )
        app = create_app(settings, http_transport=httpx.MockTransport(handler))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            token = await client.post(
                "/v1/auth/token", data={"username": "alice", "password": "strong-test-password"}
            )
            assert token.status_code == 200
            client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
            yield app, client, requests, responses

    return factory


async def settle(app, session_id):
    import asyncio

    task = app.state.harness.tasks.get(session_id)
    if task:
        await task
        await asyncio.sleep(0)
    return app.state.db.session(session_id)
