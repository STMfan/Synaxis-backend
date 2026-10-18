"""0.3.1: deterministic display repair, Off stages and short cached prefixes."""

import json
from decimal import Decimal

import httpx
import pytest
from conftest import formatted, llm_text, plan_payload, settle

from core.configuration import STAGES
from core.presentation import parse_expert, unwrap_document
from core.prompts import COMMON, FORMATTER, PLANNER, expert_prompt
from domain.errors import HarnessError
from llm.contracts import CallContext
from llm.deepseek import DeepSeekClient
from storage.budget import reservation_cost
from storage.database import Database

FENCE = chr(96) * 3


@pytest.mark.parametrize(
    "text",
    [
        r"若 \(0<v_1<\sqrt{gL}\)，杆向上支撑，\(N=mg-\frac{mv^2}{L}\)。",
        r"\[f(x)=\begin{cases}x^2,&x<1,\\[4pt]2x-1,&x\ge1.\end{cases}\]",
        r"\(2^2-5\cdot2+6=0\ \checkmark\)",
        r"\(2+\Delta x\xrightarrow{\Delta x\to0^-}2\)，\(A\nRightarrow B\)。",
        r"\(a\xleftarrow[b]{c}d\)",
        r"\[S=\frac{\sqrt3}{6}\tag{1}\]",
        "[教育部](https://www.moe.gov.cn/)的资料。",
        "{1,2,3} 是有限集合。",
    ],
)
def test_renderable_content_does_not_need_formatter(text):
    assert parse_expert(text, []).blocks[0].text == text


@pytest.mark.parametrize("language", ["markdown", "md", "MARKDOWN"])
def test_document_fence_removed_without_changing_real_code(language):
    content = "**答案**：两根为2、3。"
    assert parse_expert(f"{FENCE}{language}\n{content}\n{FENCE}", []).blocks[0].text == content
    code = f"{FENCE}python\nprint(1)\n{FENCE}"
    assert unwrap_document(code) == code
    two = f"{FENCE}md\n第一段\n{FENCE}\n第二段\n{FENCE}"
    assert unwrap_document(two) == two


def test_nested_presentation_markdown_normalized_and_fenced_json_accepted():
    value = {
        "blocks": [
            {
                "type": "branch",
                "selected": "a",
                "tabs": [
                    {
                        "id": "a",
                        "label": "答案",
                        "blocks": [{"type": "markdown", "text": f"{FENCE}md\n**结论**\n{FENCE}"}],
                    }
                ],
            }
        ]
    }
    source = f"{FENCE}json\n{json.dumps(value)}\n{FENCE}"
    assert list(parse_expert(source, []).leaves())[0].text == "**结论**"


@pytest.mark.parametrize(
    "text",
    [
        r"\(0<x<1\)<script>alert(1)</script>",
        r"\[a\\[4pt]b",  # row spacing must not hide an unclosed formula
        r"\(a\xrightarrow\)",
        r"\[a\tag\]",
        r"\(a\xleftarrow[unfinished{b}\)",
        r"\(\href{https://bad}{x}\)",
        '公式：{"type":"latex","source":"x=1"}',
        f"{FENCE}markdown\n<script>x</script>\n{FENCE}",
        f'{FENCE}json\n{{"blocks":[{{"type":"localfile","artifact_id":"{"a" * 32}","label":"x"}}]}}\n{FENCE}',
    ],
)
def test_normalization_does_not_bypass_security_or_completeness(text):
    with pytest.raises((ValueError, HarnessError)):
        parse_expert(text, [])


async def test_physics_regression_completes_without_paid_formatter(app_factory):
    answer = r"若 \(0<v_1<\sqrt{gL}\)，杆对球向上支撑。"
    async with app_factory([plan_payload("Nomos", "physics"), llm_text(answer)]) as (
        app,
        client,
        requests,
        remaining,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "杆模型受力分析"})).json()["id"]
        row = await settle(app, sid)
        assert row["status"] == "completed"
        assert row["state"]["presentations"][0]["blocks"][0]["text"] == answer
        assert len(requests) == 2 and not remaining
        assert not any(e["kind"] == "warning" for e in app.state.db.events(sid))
        assert requests[0]["body"]["thinking"] == {"type": "disabled"}
        assert "output_config" not in requests[0]["body"]
        assert requests[1]["body"]["output_config"]["effort"] == "low"


async def test_formatter_off_only_when_needed_and_normalizes_json_fence(app_factory):
    value = formatted("**结论**：一半。")
    text = value["content"][0]["text"]
    value["content"][0]["text"] = f"{FENCE}json\n{text}\n{FENCE}"
    async with app_factory([plan_payload(), llm_text("<b>一半</b>"), value]) as (
        app,
        client,
        requests,
        remaining,
    ):
        sid = (await client.post("/v1/sessions", json={"message": "解释一半"})).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        formatter = requests[-1]["body"]
        if app.state.settings.process_provider == "siliconflow":
            assert formatter["enable_thinking"] is False
        else:
            assert formatter["thinking"] == {"type": "disabled"}
        assert "output_config" not in formatter
        assert len(requests) == 3 and not remaining


def test_compact_stable_prompts_keep_dynamic_inputs_outside_prefix():
    assert STAGES["Planner"].effort == STAGES["Formatter"].effort == "off"
    assert STAGES["Planner"].max_tokens == 393216
    assert PLANNER.startswith(COMMON)
    assert FORMATTER.startswith(STAGES["Formatter"].instructions)
    assert COMMON not in FORMATTER
    base = expert_prompt("Euclid", [], allow_max=False)
    assert expert_prompt("Euclid", ["Render", "PythonBox"]).startswith(base)
    assert expert_prompt("Euclid", ["Render", "PythonBox"]) == expert_prompt(
        "Euclid", ["PythonBox", "Render"]
    )
    assert '"$defs"' not in base
    assert "必要" in base and "代数" in base


@pytest.mark.parametrize("stage", ["planner", "formatter", "search", "assistant"])
async def test_all_stages_cap_official_ceiling_to_affordable_budget(settings, stage):
    db = Database(settings.db_path)
    db.initialize()
    settings.session_budget_cny = 0.1
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=llm_text("完成"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await DeepSeekClient(settings, db, http).complete(
            CallContext(1, "limited", stage),
            system="x",
            messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            model=settings.main_model,
            effort="off",
            max_tokens=393216,
        )
    assert 256 <= bodies[0]["max_tokens"] < 393216
    assert reservation_cost(settings, settings.main_model, bodies[0]) <= Decimal("0.1")
    budget = db.budget_snapshot("limited", 1, settings)
    assert Decimal(budget["reserved"]) == Decimal(budget["uncertain"]) == 0
