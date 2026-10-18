import httpx
import pytest
from conftest import llm_text, plan_payload, settle

from api.middleware import BodyLimit
from domain.errors import HarnessError
from tools.pythonbox import validate_python


async def test_chunked_body_limit():
    dispatched = []

    async def downstream(scope, receive, send):
        dispatched.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def chunks():
        yield b"12345"
        yield b"6789"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=BodyLimit(downstream, limit=8)), base_url="http://test"
    ) as client:
        assert (await client.post("/", content=chunks())).status_code == 413 and not dispatched
        assert (await client.post("/", content=b"small")).status_code == 200


@pytest.mark.parametrize(
    "code",
    [
        "import svg3d as s\ns.Engine([]).render('/etc/passwd')",
        "import svg3d as s\ns.Engine([]).render('/work/output/output.svg',filename='/etc/passwd')",
        "import svglib.svglib as v\nv.svg2rlg('/etc/passwd')",
    ],
)
def test_svg3d_cannot_write_or_read_host_files(code):
    with pytest.raises(HarnessError):
        validate_python(code, [])


def test_svg3d_and_svglib_safe_output():
    validate_python(
        "import svg3d as s\nimport svglib.svglib as v\nengine=s.Engine([])\nengine.render('/work/output/output.svg',size=(300,300))\ndrawing=v.svg2rlg('/work/output/output.svg')\nprint(drawing.width)",
        [],
    )


async def test_output_truncation_fails_expert(app_factory):
    async with app_factory([plan_payload(), llm_text("incomplete", stop_reason="max_tokens")]) as (
        app,
        client,
        requests,
        _,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        row = await settle(app, sid)
        assert row["state"]["error"] == "EXPERT_OUTPUT_INCOMPLETE" and len(requests) == 2


async def test_expert_step_limit(app_factory, settings):
    settings.max_steps = 1
    payload = {
        "content": [
            {"type": "tool_use", "id": "t1", "name": "Syllabus", "input": {"subject": "math", "query": "x"}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    async with app_factory([plan_payload(), payload]) as (app, client, _, _):
        sid = (await client.post("/v1/sessions", json={"message": "x"})).json()["id"]
        assert (await settle(app, sid))["state"]["error"] == "STEP_LIMIT_REACHED"
