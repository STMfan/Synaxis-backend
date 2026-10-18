from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from auth.security import decode_token, hash_password, load_jwt_secret, token_for, verify_password
from core.prompts import COMMON, EXPERTS, expert_prompt
from domain.errors import HarnessError
from domain.schemas import Presentation
from domain.validation import validate_html, validate_latex, validate_svg
from llm.contracts import CallContext
from llm.deepseek import DeepSeekClient, parse_search, token_cost
from storage.database import Database, now
from tools.pythonbox import validate_python


def test_passwords_and_persistent_jwt(settings):
    encoded = hash_password("a-secure-password")
    assert verify_password(encoded, "a-secure-password")
    assert not verify_password(encoded, "wrong") and not verify_password("bad-hash", "x")
    with pytest.raises(ValueError):
        hash_password("short")
    settings.jwt_secret = SecretStr("")
    first = load_jwt_secret(settings)
    assert first == load_jwt_secret(settings)
    token = token_for({"id": 1, "token_version": 3}, first, 1)
    assert decode_token(token, first)["ver"] == 3


def test_pricing_cache_no_double_count_unknown(settings):
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 10,
        "output_tokens": 50,
    }
    assert token_cost(settings, "deepseek-flash", usage) == Decimal("0.000099000")
    settings.pricing_mode = "off_peak"
    assert token_cost(settings, "deepseek-flash", usage) == Decimal("0.000049500")
    assert token_cost(settings, "unknown-model", usage) is None


def test_search_structured_dedup_and_citations():
    payload = {
        "content": [
            {
                "type": "text",
                "text": "untrusted prose https://fake",
                "citations": [{"url": "https://source", "cited_text": "excerpt"}],
            },
            {
                "type": "web_search_tool_result",
                "content": [
                    {
                        "type": "web_search_result",
                        "url": "https://source",
                        "title": "Source",
                        "page_age": "today",
                    },
                    {"type": "web_search_result", "url": "https://source"},
                    {"type": "web_search_result", "url": "https://second"},
                ],
            },
        ]
    }
    result = parse_search(payload, limit=1)
    assert result["truncated"] and result["sources"] == [
        {"url": "https://source", "title": "Source", "published_at": "today", "snippet": "excerpt"}
    ]
    with pytest.raises(HarnessError, match="SEARCH_NO_RESULT_BLOCK"):
        parse_search({"content": [{"type": "text", "text": "https://invented"}]})
    with pytest.raises(HarnessError, match="SEARCH_SERVER_ERROR"):
        parse_search(
            {
                "content": [
                    {"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error"}}
                ]
            }
        )


@pytest.mark.parametrize(
    "source",
    [
        "<script>x</script>",
        '<div onclick="x">x</div>',
        '<iframe src="file:///etc/passwd"></iframe>',
        '<style>@import "https://evil";</style>',
        "<style>a{background:u\\72l(https://evil)}</style>",
        '<meta http-equiv="refresh" content="0;url=https://evil">',
    ],
)
def test_html_rejects_executable_and_external_content(source):
    with pytest.raises(HarnessError):
        validate_html(source)


@pytest.mark.parametrize(
    "source",
    [
        "<svg><script>x</script></svg>",
        "<svg><foreignObject/></svg>",
        '<svg><use href="https://evil"/></svg>',
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]><svg>&x;</svg>',
    ],
)
def test_svg_rejects_active_content(source):
    with pytest.raises(HarnessError):
        validate_svg(source)


def test_static_html_svg_and_tex_acceptance():
    validate_html(
        "<style>.x{overflow:auto;transform:rotateX(30deg)}</style><details><summary>提示</summary><div>文字</div></details>"
    )
    validate_svg(
        '<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"/></defs><rect fill="url(#g)"/></svg>'
    )
    validate_latex(r"\frac{-b\pm\sqrt{b^2-4ac}}{2a}")
    with pytest.raises(HarnessError):
        validate_latex(r"\href{https://evil}{x}")


@pytest.mark.parametrize(
    "code",
    [
        "import os\nos.system('id')",
        "import numpy as np\nnp.load('/etc/passwd')",
        "open('/secret')",
        "print((1).__class__)",
        "exec('x')",
        "eval('1')",
        "while True: pass",
        "import numpy as np\nf=np.load\nf('x')",
        "from PIL import Image\nImage.open('/etc/passwd')",
        "from PIL import Image\nImage.new('RGB',(1,1)).save('/tmp/out.png')",
    ],
)
def test_python_ast_rejects_escape(code):
    with pytest.raises(HarnessError):
        validate_python(code, [])


def test_python_ast_numpy_pillow_render_input():
    artifact_id = "a" * 32
    validate_python("import numpy as np\nprint(np.sum(np.arange(5)))", [])
    validate_python(
        f"from PIL import Image\nimg=Image.open('/work/input/{artifact_id}.png')\nimg=img.resize((40,30))\nimg.save('/work/output/output.png')",
        [artifact_id],
    )


def test_all_output_types_and_branch_constraints():
    p = Presentation.model_validate(
        {
            "blocks": [
                {"type": "markdown", "text": "提示"},
                {"type": "latex", "source": "x^2"},
                {"type": "svg", "source": "<svg/>"},
                {"type": "html", "source": "<div>卡片</div>"},
                {"type": "question", "id": "q1", "prompt": "你的思路？"},
                {
                    "type": "branch",
                    "selected": "hint",
                    "tabs": [
                        {"id": "hint", "label": "提示", "blocks": [{"type": "markdown", "text": "提示"}]}
                    ],
                },
                {"type": "localfile", "artifact_id": "a" * 32, "label": "日志"},
            ]
        }
    )
    assert len(p.blocks) == 7
    with pytest.raises(ValidationError):
        Presentation.model_validate(
            {
                "blocks": [
                    {
                        "type": "branch",
                        "selected": "missing",
                        "tabs": [
                            {"id": "hint", "label": "提示", "blocks": [{"type": "markdown", "text": "x"}]}
                        ],
                    }
                ]
            }
        )


def test_prompt_prefix_and_expert_routing():
    assert len(EXPERTS) == 8
    assert all(expert_prompt(name, []).startswith(COMMON) for name in EXPERTS)
    assert expert_prompt("Turing", ["Render"]).startswith(expert_prompt("Turing", [], allow_max=False))


def test_retention_preserves_users_and_pending(settings):
    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO users(username,password_hash,created_at) VALUES('a','hash',?)", (now(),))
    old = db.begin_call(1, "s", "expert", "m", {})
    pending = db.begin_call(1, "s", "expert", "m", {})
    db.finish_call(old, cost="0")
    with db.connection(True) as c:
        c.execute(
            "UPDATE calls SET created_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=366)).isoformat(), old),
        )
    stats = db.compact_ledger(365, 65536)
    assert stats["deleted"] == 1 and db.user(username="a")
    assert [r["id"] for r in db.usage(1)["items"]] == [pending]
    # Physical size cleanup, not just SUM(length(request)).
    for _ in range(20):
        cid = db.begin_call(1, "s", "expert", "m", {"data": "x" * 10000})
        db.finish_call(cid, cost="0")
    stats = db.compact_ledger(365, 65536)
    assert stats["bytes"] <= 65536 and db.user(username="a")


def test_recovery_keeps_waiting_and_marks_interrupted(settings):
    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as c:
        c.execute("INSERT INTO users(username,password_hash,created_at) VALUES('a','hash',?)", (now(),))
    queued = db.create_session(1, {})
    waiting = db.create_session(1, {})
    db.save_session(waiting, "waiting", db.session(waiting)["state"])
    db.begin_call(1, queued, "expert", "m", {})
    db.recover()
    assert db.session(queued)["status"] == "failed" and db.session(waiting)["status"] == "waiting"
    assert db.usage(1)["items"][0]["status"] == "interrupted"


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(401),
        httpx.Response(302, headers={"Location": "https://evil"}),
        httpx.Response(200, text="invalid"),
        httpx.Response(200, json={"content": "invalid"}),
    ],
)
async def test_provider_failure_logged_no_redirect_or_retry(settings, reply):
    db = Database(settings.db_path)
    db.initialize()
    requests = []

    def handler(request):
        requests.append(request)
        return reply

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as http:
        llm = DeepSeekClient(settings, db, http)
        with pytest.raises(HarnessError):
            await llm.complete(
                CallContext(1, "s", "expert"),
                system="stable",
                messages=[{"role": "user", "content": "q"}],
                model="deepseek-flash",
            )
    calls = db.usage(1)["items"]
    assert len(requests) == 1 and calls[0]["status"] == "failed" and calls[0]["cost"] is None
    assert "unit-test-key" not in str(calls)
