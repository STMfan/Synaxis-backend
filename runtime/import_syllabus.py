"""Import the supplied bundle once; no extraction, re-embedding or source changes."""

import argparse
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from core.settings import Settings
from storage.syllabus import DATA_FILES, MODEL_FILES, SyllabusIndex, digest, verify_imported_files


def model_source(source, index):
    """Accept a verified portable bundle or the original local HF cache layout."""
    receipt_path = source / "import.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt["schema_version"] != "1.0"
            or receipt["edition"] != index.manifest["edition"]
            or receipt["chunks"] != len(index.chunks)
            or receipt["sections"] != len(index.sections)
        ):
            raise ValueError("Portable bundle receipt does not match its index")
        snapshot = source / "models"
        verify_imported_files(snapshot, MODEL_FILES, "models")
        revision = receipt["model_revision"]
    else:
        model_cache = source / "models/models--Qdrant--bge-small-zh-v1.5"
        revision = (model_cache / "refs/main").read_text(encoding="utf-8").strip()
        snapshot = model_cache / "snapshots" / revision
    if not isinstance(revision, str) or len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("Invalid local model revision")
    if not all((snapshot / name).is_file() for name in MODEL_FILES):
        raise ValueError("Local curriculum model missing")
    return snapshot, revision


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
    snapshot, revision = model_source(source, index)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".syllabus-import-", dir=destination.parent) as temporary:
        staged = Path(temporary).resolve() / "bundle"
        if not staged.is_relative_to(destination.parent):
            raise ValueError("Import staging escaped destination")
        (staged / "data").mkdir(parents=True)
        (staged / "models").mkdir()
        for name in DATA_FILES:
            shutil.copyfile(source / "data" / name, staged / "data" / name)
        # Keep optional page provenance, but do not ship historical validation reports.
        if (source / "data/source_pages.jsonl").is_file():
            verify_imported_files(source / "data", ["source_pages.jsonl"], "data")
            shutil.copyfile(source / "data/source_pages.jsonl", staged / "data/source_pages.jsonl")
        # Dereference the HF cache's links into portable regular files. No venv,
        # extraction code, duplicate ONNX blobs or absolute cache links are copied.
        for name in MODEL_FILES:
            shutil.copyfile(snapshot / name, staged / "models" / name)
        notice = snapshot / "MODEL-LICENSE.txt"
        if notice.is_file():
            if (source / "import.json").is_file():
                verify_imported_files(snapshot, [notice.name], "models")
        else:
            notice = Path(__file__).resolve().parents[1] / "deploy/syllabus-model-LICENSE.txt"
        shutil.copyfile(notice, staged / "models/MODEL-LICENSE.txt")
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
