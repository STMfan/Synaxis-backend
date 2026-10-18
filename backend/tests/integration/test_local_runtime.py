"""Real local acceptance; synthetic provider, no external model or student content."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

from auth.security import hash_password
from core.runtime_config import CONFIG_ROOT
from core.version import VERSION

pytestmark = pytest.mark.integration
PASSWORD = "synthetic-admin-password"
STUDENT_PASSWORD = "synthetic-student-password"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024"><path d="M 100 900 Q 512 100 924 900" stroke="blue" stroke-width="4" fill="none"/></svg>'


class Gateway(BaseHTTPRequestHandler):
    records = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.records.append(body)
        tools, text = None, ""
        if not body.get("tools"):
            content = json.loads(body["messages"][-1]["content"])
            if "answer" in content:
                presentation = json.loads(content["answer"])
                for block in presentation["blocks"]:
                    if block["type"] == "latex":
                        block["source"] = "x=2"
                text = json.dumps(presentation, ensure_ascii=False)
            else:
                text = json.dumps(
                    {
                        "subject": "math",
                        "expert": "Turing",
                        "task": "synthetic drawing",
                        "problem_type": "geometry",
                        "skills": ["Render", "PythonBox"],
                    }
                )
        else:
            count = sum(len(m.get("tool_calls", [])) for m in body["messages"])
            if count == 0:
                tools = ("Render", {"source": SVG, "kind": "svg", "width": 1024, "height": 1024})
                text = "先画出平滑曲线。"
            elif count == 1:
                result = json.loads(
                    next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool")
                )
                identifier = result["artifact_id"]
                code = (
                    'import numpy as np\nfrom PIL import Image\nprint(int(np.sum(np.array([2,3]))))\nim=Image.open("/work/input/'
                    + identifier
                    + '.png")\nim.save("/work/output/output.png")'
                )
                tools = ("PythonBox", {"code": code, "input_artifact_ids": [identifier]})
            elif count == 2:
                result = json.loads(
                    next(m["content"] for m in reversed(body["messages"]) if m["role"] == "tool")
                )
                identifier = next(f["artifact_id"] for f in result["files"] if f["mime"] == "image/png")
                text = json.dumps(
                    {
                        "blocks": [
                            {"type": "markdown", "text": "看这张图。"},
                            {"type": "localfile", "artifact_id": identifier, "label": "示意图"},
                            {"type": "latex", "source": r"x\notACommand{}", "display": True},
                            {"type": "markdown", "text": "结论见公式。"},
                        ]
                    },
                    ensure_ascii=False,
                )
                tools = ("AskStudent", {"id": "q1", "prompt": "是否理解？", "options": ["理解", "需要提示"]})
            else:
                text = "补充：观察图中曲线再检查条件。"
        chunks = []
        if body.get("tools"):
            chunks.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"reasoning_content": "synthetic calculation trace"},
                            "finish_reason": None,
                        }
                    ]
                }
            )
        middle = len(text) // 2
        for fragment in (text[:middle], text[middle:]):
            if fragment:
                chunks.append(
                    {"choices": [{"index": 0, "delta": {"content": fragment}, "finish_reason": None}]}
                )
        if tools:
            name, arguments = tools
            arguments = json.dumps(arguments, ensure_ascii=False)
            for index, fragment in enumerate(
                (arguments[: len(arguments) // 2], arguments[len(arguments) // 2 :])
            ):
                call = {"index": 0, "function": {"arguments": fragment}}
                if index == 0:
                    call.update(id=f"call-{name}", type="function")
                    call["function"]["name"] = name
                chunks.append(
                    {"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}]}
                )
        chunks.append(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if tools else "stop"}]}
        )
        chunks.append(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            }
        )
        wire = (
            "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks)
            + "data: [DONE]\n\n"
        )
        raw = wire.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


async def await_status(client, sid, status):
    async with asyncio.timeout(45):
        while True:
            response = await client.get(f"/v1/sessions/{sid}")
            value = response.json()
            if value.get("status") == status:
                return value
            assert value.get("status") not in ("failed", "cancelled"), value.get("error")
            await asyncio.sleep(0.1)


async def test_local_https_admin_teaching_sandbox_and_restart(tmp_path):
    assert sys.platform == "linux", "Run integration acceptance in Linux/WSL"
    binary = os.environ["SYNAXIS_TEST_NSJAIL"]
    rootfs = os.environ["SYNAXIS_TEST_ROOTFS"]
    assert Path(binary).is_file() and Path(rootfs, "opt/python/bin/python3.12").is_file()
    data, directory = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    shutil.copytree(CONFIG_ROOT, directory)
    (data / "admin-password.hash").write_text(hash_password(PASSWORD))
    empty_env = tmp_path / ".env"
    empty_env.write_text("")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    Gateway.records = []
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    network_path = directory / "runtime/network.json"
    network = json.loads(network_path.read_text())
    network.update(host="127.0.0.1", port=port, tls_certfile=str(cert), tls_keyfile=str(key))
    network_path.write_text(json.dumps(network))
    sandbox_path = directory / "runtime/sandbox.json"
    sandbox = json.loads(sandbox_path.read_text())
    sandbox.update(nsjail_root=rootfs, nsjail_binary=binary)
    sandbox_path.write_text(json.dumps(sandbox))
    providers_path = directory / "models/providers.json"
    providers = json.loads(providers_path.read_text())
    providers["roles"] = {role: "local-test" for role in ("main", "literature", "process")}
    providers["providers"]["local-test"] = {
        "protocol": "openai_chat",
        "base_url": f"http://127.0.0.1:{gateway.server_port}/v1",
        "output_limits": {"synthetic-teacher": 8192},
        "context_limits": {"synthetic-teacher": 131072},
        "price_rates_cny": {"synthetic-teacher": [0.1, 1, 2]},
        "reasoning_parameter": "reasoning_effort",
        "reasoning_values": {"off": "none", "low": "low", "high": "high", "max": "max"},
        "supports_json_mode": True,
    }
    providers_path.write_text(json.dumps(providers))
    environment = os.environ.copy()
    environment.update(SYNAXIS_CONFIG_DIR=str(directory), SYNAXIS_ENV_FILE=str(empty_env), DATA_DIR=str(data))
    for role in ("MAIN", "LITERATURE", "PROCESS"):
        environment.update(
            {
                f"TEXT_{role}_PROVIDER": "local-test",
                f"TEXT_{role}_MODEL": "synthetic-teacher",
                f"TEXT_{role}_API_KEY": "synthetic-key",
            }
        )
    project = Path(__file__).resolve().parents[3]
    log_path = tmp_path / "service.log"
    base = f"https://127.0.0.1:{port}"
    with log_path.open("w") as logs:
        process = subprocess.Popen(
            [sys.executable, str(project / "main.py")],
            env=environment,
            stdout=logs,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async with httpx.AsyncClient(base_url=base, verify=False, timeout=50) as client:
                async with asyncio.timeout(30):
                    while True:
                        assert process.poll() is None, log_path.read_text()[-3000:]
                        try:
                            if (await client.get("/health")).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.1)
                assert (await client.get("/health")).json()["version"] == VERSION
                assert (await client.get("/openapi.json?lang=en")).json()["info"]["version"] == VERSION
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context(
                        ignore_https_errors=True, viewport={"width": 1024, "height": 900}
                    )
                    page = await context.new_page()
                    errors = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    page.on("dialog", lambda d: d.accept())
                    await page.goto(base + "/admin")
                    await page.fill("#admin-password", PASSWORD)
                    await page.click("#login-form button")
                    await page.wait_for_selector("#panel:not([hidden])")
                    await page.click("summary:has-text('新增用户')")
                    await page.fill("#new-name", "student")
                    await page.fill("#new-password", STUDENT_PASSWORD)
                    await page.click("#create-form button")
                    await page.wait_for_selector("#users tr:has-text('student')")
                    await page.screenshot(path=str(tmp_path / "admin-desktop.png"), full_page=True)
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(tmp_path / "admin-mobile.png"), full_page=True)
                    login = await client.post(
                        "/v1/auth/token", data={"username": "student", "password": STUDENT_PASSWORD}
                    )
                    client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                    created = await client.post(
                        "/v1/sessions",
                        json={"message": "绘制曲线并说明", "subject": "math"},
                        headers={"Idempotency-Key": "synthetic-request"},
                    )
                    assert created.status_code == 202
                    sid = created.json()["id"]
                    last_id = 0
                    async with client.stream("GET", f"/v1/sessions/{sid}/events") as stream:
                        async for line in stream.aiter_lines():
                            if line.startswith("id:"):
                                last_id = int(line[3:])
                                break  # Real subscriber disconnect must not cancel the task.
                    waiting = await await_status(client, sid, "waiting")
                    replay = await client.post(
                        "/v1/sessions",
                        json={"message": "绘制曲线并说明", "subject": "math"},
                        headers={"Idempotency-Key": "synthetic-request"},
                    )
                    assert replay.json()["id"] == sid and replay.headers["Idempotency-Replayed"] == "true"
                    events = await client.get(
                        f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": str(last_id)}
                    )
                    assert "event: reasoning_delta" in events.text and "event: waiting" in events.text
                    blocks = waiting["presentations"][-1]["blocks"]
                    picture = next(
                        b for b in blocks if b["type"] == "localfile" and b.get("label") == "示意图"
                    )
                    assert 0 < blocks.index(picture) < len(blocks) - 1
                    image = await client.get("/v1/artifacts/" + picture["artifact_id"])
                    assert image.content.startswith(b"\x89PNG")
                    assert next(b for b in blocks if b["type"] == "latex")["source"] == "x=2"
                    formatter = next(
                        r
                        for r in Gateway.records
                        if "answer" in json.loads(r["messages"][-1]["content"]) and not r.get("tools")
                    )
                    assert all(isinstance(m["content"], str) for m in formatter["messages"])
                    assert (
                        await client.post(
                            f"/v1/sessions/{sid}/answers", json={"question_id": "q1", "answer": "理解"}
                        )
                    ).status_code == 202
                    await await_status(client, sid, "completed")
                    assert (
                        await client.post(
                            f"/v1/sessions/{sid}/messages",
                            json={"message": "再解释一下"},
                            headers={"Idempotency-Key": "followup"},
                        )
                    ).status_code == 202
                    await await_status(client, sid, "completed")
                    assert (await client.get("/v1/sessions")).json()["items"]
                    assert (await client.get(f"/v1/sessions/{sid}/messages")).json()["items"]
                    assert "request" not in json.dumps((await client.get("/v1/usage")).json())
                    await page.locator("#users tr:has-text('student') button:has-text('编辑')").click()
                    await page.fill("#edit-name", "student-renamed")
                    await page.click("#edit-form button[type='submit'], #edit-form button:not([type])")
                    await page.wait_for_selector("#users tr:has-text('student-renamed')")
                    assert (await client.get("/v1/me")).status_code == 401
                    network["cors_origins"] = ["https://front.example"]
                    await page.fill("#network", json.dumps(network))
                    await page.click("#save-network")
                    await page.locator("#status").filter(has_text="已保存").wait_for()
                    await page.click("#restart")
                    async with asyncio.timeout(30):
                        while True:
                            try:
                                preflight = await client.options(
                                    "/v1/sessions",
                                    headers={
                                        "Origin": "https://front.example",
                                        "Access-Control-Request-Method": "POST",
                                        "Access-Control-Request-Headers": "authorization,content-type",
                                    },
                                )
                                if (
                                    preflight.headers.get("access-control-allow-origin")
                                    == "https://front.example"
                                ):
                                    break
                            except httpx.TransportError:
                                pass
                            await asyncio.sleep(0.2)
                    assert (await page.request.get(base + "/admin/api/session")).status == 401
                    assert not errors
                    await browser.close()
        finally:
            process.send_signal(2)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            gateway.shutdown()
            gateway.server_close()
