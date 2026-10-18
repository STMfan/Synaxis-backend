"""Import the supplied bundle once; no extraction, re-embedding or source changes."""

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from core.settings import Settings
from storage.syllabus import DATA_FILES, MODEL_FILES, SyllabusIndex, digest


def import_bundle(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError(
            "Destination already exists; use a new directory and update syllabus.json while stopped"
        )
    if not (source / "data").is_dir():
        raise ValueError("Source must contain the supplied data/ and models/ directories")
    # Verify original indexes first, and original PDFs when present.
    index = SyllabusIndex(source / "data")
    verified_pdfs = 0
    for document in index.manifest["documents"]:
        name = document["source_file"]
        if Path(name).name != name:
            raise ValueError("Invalid source PDF name")
        pdf = source / name
        if pdf.is_file():
            if digest(pdf) != document["source_sha256"]:
                raise ValueError("Source PDF checksum mismatch")
            verified_pdfs += 1
    model_cache = source / "models/models--Qdrant--bge-small-zh-v1.5"
    revision = (model_cache / "refs/main").read_text(encoding="utf-8").strip()
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Invalid local model revision")
    snapshot = model_cache / "snapshots" / revision
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".syllabus-import-", dir=destination.parent) as temporary:
        staged = Path(temporary).resolve() / "bundle"
        if not staged.is_relative_to(destination.parent):
            raise ValueError("Import staging escaped destination")
        (staged / "data").mkdir(parents=True)
        (staged / "models").mkdir()
        for name in DATA_FILES + ("source_pages.jsonl", "validation.json"):
            shutil.copyfile(source / "data" / name, staged / "data" / name)
        # Dereference the HF cache's links into portable regular files. No venv,
        # extraction code, duplicate ONNX blobs or absolute cache links are copied.
        for name in MODEL_FILES:
            shutil.copyfile(snapshot / name, staged / "models" / name)
        SyllabusIndex(staged / "data")
        receipt = {
            "schema_version": "1.0",
            "edition": index.manifest["edition"],
            "model_revision": revision,
            "chunks": len(index.chunks),
            "sections": len(index.sections),
            "pdf_hashes_verified": verified_pdfs,
            "files": {
                str(p.relative_to(staged)).replace("\\", "/"): digest(p)
                for p in sorted(staged.rglob("*"))
                if p.is_file()
            },
        }
        (staged / "import.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        staged.rename(destination)
    return receipt


def main():
    parser = argparse.ArgumentParser(description="Import local 2017 syllabus bundle / 导入本地课标包")
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    settings = Settings()
    try:
        receipt = import_bundle(args.source, args.destination or settings.data_dir / "syllabus")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        parser.exit(2, f"Import failed / 导入失败：{type(exc).__name__}\n")
    print(
        json.dumps(
            {key: receipt[key] for key in ("edition", "chunks", "sections", "pdf_hashes_verified")},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
