"""Read the supplied SQLite/NumPy corpus without rebuilding or modifying it."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np

from domain.syllabus import SUBJECTS, SyllabusChunk, SyllabusSection

MODEL = "BAAI/bge-small-zh-v1.5"
DATA_FILES = (
    "chunks.jsonl",
    "sections.jsonl",
    "curriculum.sqlite3",
    "manifest.json",
    "vectors.npy",
    "vectors.json",
)
MODEL_FILES = (
    "model_optimized.onnx",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def verify_imported_files(directory, filenames, group):
    receipt = directory.parent / "import.json"
    if receipt.is_file():
        expected = json.loads(receipt.read_text(encoding="utf-8"))["files"]
        for name in filenames:
            if digest(directory / name) != expected[f"{group}/{name}"]:
                raise ValueError("Imported curriculum bundle checksum mismatch")


def terms(text):
    result = []
    for segment in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", text.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", segment):
            result.extend(segment)
            result.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        else:
            result.append(segment)
    return list(dict.fromkeys(result))[:128]


class SyllabusIndex:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        verify_imported_files(self.directory, DATA_FILES, "data")
        self.manifest = json.loads((self.directory / "manifest.json").read_text(encoding="utf-8"))
        meta = json.loads((self.directory / "vectors.json").read_text(encoding="utf-8"))
        if (
            self.manifest["schema_version"] != "1.0"
            or "2017" not in self.manifest["edition"]
            or meta["model"] != MODEL
            or meta["dimension"] != 512
            or meta["normalization"] != "l2"
            or meta["query_encoding"] != "FastEmbed.query_embed"
            or meta["document_encoding"] != "FastEmbed.passage_embed"
            or meta["chunks_sha256"] != digest(self.directory / "chunks.jsonl")
            or meta["vectors_sha256"] != digest(self.directory / "vectors.npy")
        ):
            raise ValueError("Curriculum edition, encoding or checksums mismatch")
        chunks = [
            SyllabusChunk.model_validate(x).model_dump() for x in read_lines(self.directory / "chunks.jsonl")
        ]
        sections = [
            SyllabusSection.model_validate(x).model_dump()
            for x in read_lines(self.directory / "sections.jsonl")
        ]
        self.chunks = {x["id"]: x for x in chunks}
        self.sections = {x["id"]: x for x in sections}
        self.ids = meta["ids"]
        if (
            len(self.chunks) != len(chunks)
            or len(self.sections) != len(sections)
            or len(set(self.ids)) != len(self.ids)
            or set(self.ids) != set(self.chunks)
            or meta["count"] != len(chunks)
            or self.manifest["total_chunks"] != len(chunks)
            or self.manifest["total_sections"] != len(sections)
            or not chunks
            or not sections
        ):
            raise ValueError("Curriculum identifiers/counts mismatch")
        sources = {
            x["source_file"]: x["source_sha256"]
            for x in self.manifest["documents"]
            if x["status"] == "extracted"
        }
        for item in [*chunks, *sections]:
            if (
                sources.get(item["source"]["file"]) != item["source"]["sha256"]
                or item["subject"] not in SUBJECTS.values()
                or not item["source"]["pdf_pages"]
                or any(p < 1 for p in item["source"]["pdf_pages"])
            ):
                raise ValueError("Curriculum provenance mismatch")
        if any(
            x["section_id"] not in self.sections or self.sections[x["section_id"]]["subject"] != x["subject"]
            for x in chunks
        ):
            raise ValueError("Curriculum parent section mismatch")
        self.vectors = np.load(self.directory / "vectors.npy", allow_pickle=False)
        if (
            self.vectors.shape != (len(chunks), 512)
            or self.vectors.dtype != np.float32
            or not np.isfinite(self.vectors).all()
            or not np.allclose(np.linalg.norm(self.vectors, axis=1), 1, atol=0.001)
        ):
            raise ValueError("Invalid curriculum vectors")
        self.vectors.flags.writeable = False
        with self.connection() as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Invalid curriculum database")
            stored = db.execute("SELECT value FROM metadata WHERE key='chunks_sha256'").fetchone()
            rows = db.execute("SELECT id,subject,categories,payload FROM chunks").fetchall()
            if not stored or stored[0] != meta["chunks_sha256"] or len(rows) != len(chunks):
                raise ValueError("Stale curriculum database")
            for identifier, subject, categories, payload in rows:
                original = self.chunks.get(identifier)
                if (
                    original != json.loads(payload)
                    or subject != original["subject"]
                    or categories != "|" + "|".join(original["course_categories"]) + "|"
                ):
                    raise ValueError("Curriculum database payload mismatch")
        self.subjects = sorted({x["subject"] for x in chunks})

    def connection(self):
        # URI construction handles Windows drive letters and paths containing ?/#.
        return closing(
            sqlite3.connect((self.directory / "curriculum.sqlite3").as_uri() + "?mode=ro", uri=True)
        )

    def selected(self, subject, category):
        return {
            key: value
            for key, value in self.chunks.items()
            if (subject is None or value["subject"] == subject)
            and (category is None or category in value["course_categories"])
        }

    def lexical(self, query, count, subject, category):
        tokens = terms(query)
        if not tokens:
            return []
        match = " OR ".join('"' + token + '"' for token in tokens)
        clauses, params = [], [match]
        if subject:
            clauses.append("c.subject=?")
            params.append(subject)
        if category:
            clauses.append("instr(c.categories,?)>0")
            params.append("|" + category + "|")
        where = " AND " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            rows = db.execute(
                "SELECT c.id,bm25(search_fts) AS rank FROM search_fts JOIN chunks c ON c.id=search_fts.id "
                "WHERE search_fts MATCH ?" + where + " ORDER BY rank,c.id LIMIT ?",
                [*params, count],
            ).fetchall()
        return [(identifier, -rank) for identifier, rank in rows]

    def search(self, query, *, mode, top_k, subject=None, category=None, vector=None):
        count = max(40, top_k * 4) if mode == "hybrid" else top_k
        lexical = self.lexical(query, count, subject, category) if mode != "semantic" else []
        semantic = []
        if mode != "lexical":
            q = np.asarray(vector, dtype=np.float32)
            if q.shape != (512,) or not np.isfinite(q).all() or np.linalg.norm(q) == 0:
                raise ValueError("Invalid query vector")
            scores = self.vectors @ (q / np.linalg.norm(q))
            allowed = self.selected(subject, category)
            semantic = sorted(
                ((key, float(scores[i])) for i, key in enumerate(self.ids) if key in allowed),
                key=lambda p: (-p[1], p[0]),
            )[:count]
        if mode == "hybrid":
            fused = {}
            for ranking in (lexical, semantic):
                for rank, (identifier, _) in enumerate(ranking, 1):
                    fused[identifier] = fused.get(identifier, 0) + 1 / (60 + rank)
            ordered = sorted(fused.items(), key=lambda p: (-p[1], p[0]))[:top_k]
        else:
            ordered = lexical if mode == "lexical" else semantic
        return [
            {**self.chunks[key], "score": float(score), "retrieval_method": mode} for key, score in ordered
        ]
