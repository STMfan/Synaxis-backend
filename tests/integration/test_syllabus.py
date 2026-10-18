"""Explicit real-corpus acceptance. Never download models or call a paid API."""

import json
import os
import socket
from pathlib import Path

import pytest
from conftest import llm_text, plan_payload, settle

from domain.syllabus import SUBJECTS
from storage.syllabus import digest

pytestmark = pytest.mark.syllabus_integration


async def test_real_nine_subject_offline_retrieval_and_expert_round(app_factory, settings, monkeypatch):
    bundle = Path(
        os.environ.get("SYNAXIS_TEST_SYLLABUS", str(Path(__file__).resolve().parents[2] / "data/syllabus"))
    )
    if not (bundle / "import.json").is_file():
        pytest.fail("Import the supplied syllabus bundle before explicitly running syllabus_integration")
    settings.syllabus_data_dir = bundle / "data"
    settings.syllabus_model_cache = bundle / "models"
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    original_connect = socket.socket.connect

    def no_network(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            pytest.fail("Local syllabus retrieval attempted an external connection")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    corpus_hash = digest(bundle / "data/curriculum.sqlite3")
    tool = {
        "content": [
            {
                "type": "tool_use",
                "id": "syllabus-actual",
                "name": "Syllabus",
                "input": {
                    "subject": "math",
                    "query": "圆锥曲线与方程",
                    "course_category": "选择性必修课程",
                    "top_k": 3,
                },
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    async with app_factory(
        [plan_payload(), tool, llm_text("课标要求掌握椭圆的定义、标准方程及简单几何性质。")]
    ) as (app, client, requests, _):
        assert (await client.get("/health")).json()["syllabus"] == "ready"
        info = (await client.get("/v1/capabilities")).json()["syllabus"]
        assert info["chunks"] == 567 and info["sections"] == 261 and len(info["subjects"]) == 9
        queries = {
            "chinese": "整本书阅读与研讨",
            "english": "主题语境与语篇类型",
            "math": "圆锥曲线与方程",
            "physics": "牛顿运动定律",
            "chemistry": "氧化还原反应",
            "biology": "遗传信息的储存和传递",
            "politics": "中国特色社会主义",
            "history": "中国古代史",
            "geography": "地球运动",
        }
        for subject, query in queries.items():
            for mode in ("lexical", "semantic", "hybrid"):
                result = await client.post(
                    "/v1/syllabus/search", json={"query": query, "subject": subject, "top_k": 3, "mode": mode}
                )
                assert result.status_code == 200, result.text
                hits = result.json()["results"]
                assert hits and all(x["subject"] == SUBJECTS[subject] for x in hits)
                assert all(x["source"]["pdf_pages"] and len(x["source"]["sha256"]) == 64 for x in hits)
                section = (await client.get("/v1/syllabus/sections/" + hits[0]["section_id"])).json()
                assert section["subject"] == SUBJECTS[subject] and section["fragments"]
        long = await client.post("/v1/syllabus/search", json={"query": "函数" * 600, "mode": "semantic"})
        assert long.status_code == 400 and long.json()["code"] == "SYLLABUS_QUERY_TOO_LONG"
        sid = (
            await client.post("/v1/sessions", json={"message": "圆锥曲线的课标要求？", "subject": "math"})
        ).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        returned = json.loads(requests[2]["body"]["messages"][2]["content"][0]["content"])
        first = returned["results"][0]
        assert returned["status"] == "ok" and "椭圆" in first["text"]
        assert "选择性必修课程" in first["course_categories"]
        assert first["source"]["pdf_pages"] == [54] and first["quality_warnings"]
        assert len(app.state.db.usage(1)["items"]) == 3
    assert digest(bundle / "data/curriculum.sqlite3") == corpus_hash
