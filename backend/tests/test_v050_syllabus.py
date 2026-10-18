"""Portable retrieval, integrity, lifecycle, API and tool contracts; no real model."""

import asyncio
import json
import sqlite3
import threading
from contextlib import closing

import numpy as np
import pytest
from conftest import llm_text, plan_payload, settle

from core.version import VERSION
from domain.errors import HarnessError
from domain.syllabus import SyllabusQuery
from runtime.import_syllabus import import_bundle
from storage.syllabus import MODEL, MODEL_FILES, SyllabusIndex, digest, terms, verify_imported_files
from tools.syllabus import LocalEmbedder, SyllabusService


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    data = root / "data"
    data.mkdir(parents=True)
    chunks, sections = [], []
    for i, (subject, category, text) in enumerate(
        [
            ("数学", "必修课程", "函数的概念"),
            ("数学", "选择性必修课程", "圆锥曲线与方程"),
            ("生物学", "必修课程", "遗传信息"),
        ]
    ):
        source = {
            "file": f"{subject}.pdf",
            "sha256": "a" * 64,
            "pdf_pages": [20 + i],
            "pdf_page_start": 20 + i,
            "pdf_page_end": 20 + i,
            "printed_pages": [10 + i],
        }
        common = {
            "schema_version": "1.0",
            "document_id": f"doc{i}",
            "subject": subject,
            "course_categories": [category],
            "original_course_categories": [category, "选修课程"],
            "scope": "shared_with_elective",
            "heading_path": ["课程内容", category, text],
            "text": text,
            "source": source,
            "quality_warnings": ["synthetic formula warning"],
        }
        chunks.append(
            {
                **common,
                "id": f"chunk{i}",
                "section_id": f"section{i}",
                "context_before": "",
                "context_pdf_pages": [],
                "embedding_text": text,
            }
        )
        sections.append(
            {
                **common,
                "id": f"section{i}",
                "title": text,
                "fragments": [],
                "classification_evidence": {"rule": "synthetic shared category"},
            }
        )
    for name, values in (("chunks", chunks), ("sections", sections)):
        (data / f"{name}.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in values) + "\n", encoding="utf-8"
        )
    manifest = {
        "schema_version": "1.0",
        "edition": "2017年版（合成测试）",
        "total_chunks": 3,
        "total_sections": 3,
        "documents": [
            {"source_file": f"{name}.pdf", "source_sha256": "a" * 64, "status": "extracted"}
            for name in ("数学", "生物学")
        ],
    }
    (data / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    vectors = np.zeros((3, 512), dtype=np.float32)
    for i in range(3):
        vectors[i, i] = 1
    np.save(data / "vectors.npy", vectors)
    meta = {
        "model": MODEL,
        "dimension": 512,
        "count": 3,
        "normalization": "l2",
        "ids": [x["id"] for x in chunks],
        "query_encoding": "FastEmbed.query_embed",
        "document_encoding": "FastEmbed.passage_embed",
        "chunks_sha256": digest(data / "chunks.jsonl"),
        "vectors_sha256": digest(data / "vectors.npy"),
    }
    (data / "vectors.json").write_text(json.dumps(meta), encoding="utf-8")
    with closing(sqlite3.connect(data / "curriculum.sqlite3")) as db, db:
        db.executescript(
            "CREATE TABLE metadata(key TEXT,value TEXT); CREATE TABLE chunks(id TEXT,subject TEXT,categories TEXT,payload TEXT); CREATE VIRTUAL TABLE search_fts USING fts5(id UNINDEXED,tokens);"
        )
        db.execute("INSERT INTO metadata VALUES('chunks_sha256',?)", (meta["chunks_sha256"],))
        for chunk in chunks:
            db.execute(
                "INSERT INTO chunks VALUES(?,?,?,?)",
                (
                    chunk["id"],
                    chunk["subject"],
                    "|" + "|".join(chunk["course_categories"]) + "|",
                    json.dumps(chunk, ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO search_fts VALUES(?,?)", (chunk["id"], " ".join(terms(chunk["embedding_text"])))
            )
    for name in ("source_pages.jsonl", "validation.json"):
        (data / name).write_text("{}", encoding="utf-8")
    snapshot = root / "models/models--Qdrant--bge-small-zh-v1.5/snapshots" / ("b" * 40)
    snapshot.mkdir(parents=True)
    refs = snapshot.parents[1] / "refs"
    refs.mkdir()
    (refs / "main").write_text("b" * 40, encoding="utf-8")
    for name in MODEL_FILES:
        (snapshot / name).write_text("synthetic-not-a-model", encoding="utf-8")
    return root


class FakeEmbedder:
    def __init__(self, directory, threads):
        pass

    def vector(self, query):
        v = np.zeros(512, dtype=np.float32)
        v[1] = 1
        return v


def configure(settings, corpus):
    settings.syllabus_data_dir = corpus / "data"
    settings.syllabus_model_cache = corpus / "models"
    return settings


def test_modes_filtering_shared_scope_and_read_only_provenance(corpus):
    before = digest(corpus / "data/curriculum.sqlite3")
    index = SyllabusIndex(corpus / "data")
    vector = FakeEmbedder(None, 2).vector("x")
    for mode in ("lexical", "semantic", "hybrid"):
        hits = index.search(
            '圆锥曲线" OR *', mode=mode, top_k=3, vector=vector, subject="数学", category="选择性必修课程"
        )
        assert [x["id"] for x in hits] == ["chunk1"]
        assert hits[0]["original_course_categories"] == ["选择性必修课程", "选修课程"]
        assert hits[0]["quality_warnings"] == ["synthetic formula warning"]
        assert hits[0]["source"]["pdf_pages"] == [21]
    assert index.search("!!!", mode="lexical", top_k=2) == []
    assert digest(corpus / "data/curriculum.sqlite3") == before
    with index.connection() as db, pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM chunks")


@pytest.mark.parametrize("change", ["chunks", "vectors", "ids", "edition", "database", "parent", "norms"])
def test_stale_corrupt_or_mismatched_corpus_is_rejected(corpus, change):
    data = corpus / "data"
    meta = json.loads((data / "vectors.json").read_text(encoding="utf-8"))
    if change == "chunks":
        with (data / "chunks.jsonl").open("a") as f:
            f.write("\n")
    elif change == "vectors":
        with (data / "vectors.npy").open("ab") as f:
            f.write(b"tampered")
    elif change == "ids":
        meta["ids"][0] = "wrong"
    elif change == "edition":
        manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
        manifest["edition"] = "other edition"
        (data / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    elif change == "database":
        with closing(sqlite3.connect(data / "curriculum.sqlite3")) as db, db:
            db.execute("UPDATE chunks SET payload='{}'")
    elif change == "parent":
        (data / "sections.jsonl").write_text("", encoding="utf-8")
    elif change == "norms":
        np.save(data / "vectors.npy", np.zeros((3, 512), dtype=np.float32))
        meta["vectors_sha256"] = digest(data / "vectors.npy")
    (data / "vectors.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        SyllabusIndex(data)


async def test_service_ready_missing_model_explicit_lexical_and_disabled(settings, corpus):
    configure(settings, corpus)
    service = SyllabusService(settings, FakeEmbedder)
    service.initialize()
    assert service.info()["availability"] == "ready"
    result = await service.search(SyllabusQuery(query="圆锥曲线", subject="math"))
    assert result["status"] == "ok" and result["results"][0]["id"] == "chunk1"
    assert service.section("section1")["classification_evidence"]
    assert service.section("missing") is None
    service = SyllabusService(settings)
    service.initialize()
    assert service.info()["reason"] == "SYLLABUS_MODEL_UNAVAILABLE"
    assert (await service.search(SyllabusQuery(query="圆锥曲线")))["status"] == "unavailable"
    assert (await service.search(SyllabusQuery(query="圆锥曲线", mode="lexical")))["count"] == 1
    settings.syllabus_enabled = False
    disabled = SyllabusService(settings, lambda *args: pytest.fail("No model load"))
    disabled.initialize()
    assert disabled.info()["availability"] == "disabled"
    assert (await disabled.search(SyllabusQuery(query="x")))["code"] == "SYLLABUS_DISABLED"


def test_missing_or_invalid_database_fails_closed(settings, corpus):
    service = SyllabusService(settings)
    service.initialize()
    assert service.info()["reason"] == "SYLLABUS_NOT_INDEXED"
    configure(settings, corpus)
    (corpus / "data/curriculum.sqlite3").write_bytes(b"not a sqlite database")
    service = SyllabusService(settings)
    service.initialize()
    assert service.info()["reason"] == "SYLLABUS_INDEX_INVALID"


def test_import_is_portable_atomic_and_does_not_overwrite(corpus, tmp_path, monkeypatch):
    target = tmp_path / "imported"
    original = digest(corpus / "data/curriculum.sqlite3")
    receipt = import_bundle(corpus, target)
    assert receipt["chunks"] == 3 and receipt["pdf_hashes_verified"] == 0
    assert all((target / "models" / name).is_file() for name in MODEL_FILES)
    assert digest(target / "data/curriculum.sqlite3") == original
    assert (target / "import.json").is_file()
    with pytest.raises(ValueError, match="exists"):
        import_bundle(corpus, target)
    monkeypatch.setattr(
        "runtime.import_syllabus.shutil.copyfile",
        lambda *args: (_ for _ in ()).throw(OSError("synthetic failure")),
    )
    with pytest.raises(OSError):
        import_bundle(corpus, tmp_path / "failure")
    assert not (tmp_path / "failure").exists()
    source_model = next((corpus / "models").rglob("model_optimized.onnx"))
    source_model.write_text("changed source", encoding="utf-8")
    assert (target / "models/model_optimized.onnx").read_text(encoding="utf-8") == "synthetic-not-a-model"


def test_import_rejects_different_original_pdf(corpus, tmp_path):
    (corpus / "数学.pdf").write_bytes(b"different source")
    with pytest.raises(ValueError, match="PDF checksum"):
        import_bundle(corpus, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_embedder_is_local_only_and_rejects_overflow_before_inference(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import fastembed

    for name in MODEL_FILES:
        (tmp_path / name).write_text("synthetic", encoding="utf-8")
    received, truncation, inferred = [], [], []
    tokenizer = SimpleNamespace(
        no_truncation=lambda: truncation.append(False),
        encode=lambda s: SimpleNamespace(ids=list(range(513))),
        enable_truncation=lambda **kw: truncation.append(kw),
    )
    engine = SimpleNamespace(
        model=SimpleNamespace(tokenizer=tokenizer), query_embed=lambda q: inferred.append(q)
    )
    monkeypatch.setattr(fastembed, "TextEmbedding", lambda **kw: (received.append(kw), engine)[1])
    embedder = LocalEmbedder(tmp_path, 2)
    assert received[0]["local_files_only"] is True and received[0]["specific_model_path"] == str(tmp_path)
    assert received[0]["providers"] == ["CPUExecutionProvider"]
    with pytest.raises(HarnessError, match="SYLLABUS_QUERY_TOO_LONG"):
        embedder.vector("x")
    assert inferred == [] and truncation[-1] == {"max_length": 512}


async def test_cancelled_query_holds_inference_gate_until_thread_finishes(settings, corpus):
    service = SyllabusService(configure(settings, corpus), FakeEmbedder)
    service.initialize()
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(query):
        calls.append(query)
        started.set()
        assert release.wait(3)
        return FakeEmbedder(None, 2).vector(query)

    service.embedder.vector = slow
    task = asyncio.create_task(service.search(SyllabusQuery(query="first")))
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    second = asyncio.create_task(service.search(SyllabusQuery(query="second")))
    await asyncio.sleep(0.02)
    assert calls == ["first"]
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await second)["status"] == "ok"


async def test_api_auth_docs_capabilities_and_expert_tool_round(app_factory, settings, corpus, monkeypatch):
    configure(settings, corpus)
    monkeypatch.setattr("tools.syllabus.LocalEmbedder", FakeEmbedder)
    # Constructor default is bound once; inject the service factory, not a fake search result.
    monkeypatch.setattr("api.app.SyllabusService", lambda s: SyllabusService(s, FakeEmbedder))
    tool = {
        "content": [
            {
                "type": "tool_use",
                "id": "syllabus1",
                "name": "Syllabus",
                "input": {"subject": "math", "query": "圆锥曲线"},
            }
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    async with app_factory([plan_payload(), tool, llm_text("按课标要求学习圆锥曲线。")]) as (
        app,
        client,
        requests,
        _,
    ):
        assert (await client.get("/health")).json()["syllabus"] == "ready"
        cap = (await client.get("/v1/capabilities")).json()
        assert cap["syllabus"]["chunks"] == 3
        assert next(s for s in cap["skills"] if s["name"] == "Syllabus")["availability"] == "ready"
        auth = client.headers.pop("Authorization")
        assert (await client.post("/v1/syllabus/search", json={"query": "x"})).status_code == 401
        client.headers["Authorization"] = auth
        result = (
            await client.post("/v1/syllabus/search", json={"query": "圆锥曲线", "subject": "math"})
        ).json()
        section = await client.get("/v1/syllabus/sections/" + result["results"][0]["section_id"])
        assert section.status_code == 200 and section.json()["classification_evidence"]
        assert (await client.get("/v1/syllabus/sections/missing")).status_code == 404
        for query in ({"query": "  "}, {"query": "x", "top_k": 11}, {"query": "x", "subject": "invented"}):
            assert (await client.post("/v1/syllabus/search", json=query)).status_code == 422
        for lang in ("zh", "en"):
            schema = (await client.get("/openapi.json", params={"lang": lang})).json()
            assert schema["info"]["version"] == VERSION
            assert "SyllabusResult" in schema["components"]["schemas"]
        sid = (
            await client.post("/v1/sessions", json={"message": "圆锥曲线要求是什么？", "subject": "math"})
        ).json()["id"]
        assert (await settle(app, sid))["status"] == "completed"
        reply = json.loads(requests[2]["body"]["messages"][2]["content"][0]["content"])
        assert reply["status"] == "ok" and reply["results"][0]["source"]["sha256"] == "a" * 64
        assert len(app.state.db.usage(1)["items"]) == 3  # No embedding-provider call.


async def test_unavailable_api_does_not_fabricate_results(app_factory):
    async with app_factory() as (_, client, _, _):
        assert (await client.post("/v1/syllabus/search", json={"query": "x"})).status_code == 503
        assert (await client.get("/v1/syllabus/sections/missing")).status_code == 503


@pytest.mark.parametrize("group,name", [("data", "sections.jsonl"), ("models", "model_optimized.onnx")])
def test_import_receipt_detects_data_or_model_changes(corpus, tmp_path, group, name):
    destination = tmp_path / "receipt-verified"
    import_bundle(corpus, destination)
    (destination / group / name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        verify_imported_files(destination / group, [name], group)


async def test_explicit_lexical_mode_does_not_load_a_model_and_no_hits_are_not_exclusion(settings, corpus):
    settings.syllabus_mode = "lexical"
    service = SyllabusService(
        configure(settings, corpus), lambda *args: pytest.fail("No model load in lexical mode")
    )
    service.initialize()
    assert service.info()["availability"] == "ready" and service.info()["modes"] == ["lexical"]
    result = await service.search(SyllabusQuery(query="!!!"))
    assert result["status"] == "ok" and result["count"] == 0 and result["message"]
    service.initialize()
    (corpus / "data/manifest.json").write_bytes(b"invalid")
    service.initialize()
    assert service.index is None and service.info()["availability"] == "unavailable"
