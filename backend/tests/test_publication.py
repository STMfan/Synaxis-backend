"""Exercise ignore rules in a temporary repository, never initialize the project repo."""

import shutil
import subprocess
from pathlib import Path


def test_publication_ignores_private_files_but_keeps_templates(tmp_path):
    root = Path(__file__).resolve().parents[2]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    shutil.copyfile(root / ".gitignore", tmp_path / ".gitignore")
    directories = ["backend"]
    if (root / "frontend" / ".gitignore").is_file():
        directories.append("frontend")
    for directory in directories:
        (tmp_path / directory).mkdir()
        shutil.copyfile(root / directory / ".gitignore", tmp_path / directory / ".gitignore")
    private = [
        "backend/.env",
        "backend/.env.production",
        "backend/service.env",
        "backend/server.key",
        "backend/server.pem",
        "backend/data/synaxis.sqlite3",
        "backend/data/calls.sqlite3-wal",
        "backend/data/syllabus/models/model_optimized.onnx",
        "backend/data/syllabus/data/chunks.jsonl",
        "backend/archive/pre-v040/manifest.json",
        "backend/test-results/log.json",
        "backend/deploy-backups/db.sqlite3",
        "backend/rootfs/opt/python/bin/python3.12",
        "backend/.venv/bin/python",
    ]
    if "frontend" in directories:
        private.extend(["frontend/.secrets/key.json", "frontend/releases/app.apk"])
    public = [
        "main.py",
        "backend/.env.example",
        "backend/config/models/providers.json",
        "backend/config/runtime/network.json",
        "backend/docs/deployment.md",
        "backend/tests/test_v040_providers.py",
        "backend/admin/admin.js",
        "backend/uv.lock",
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-z", "--stdin"],
        cwd=tmp_path,
        input=("\0".join(private + public) + "\0").encode(),
        check=True,
        capture_output=True,
    )
    ignored = set(result.stdout.decode().strip("\0").split("\0"))
    assert ignored == set(private)
