"""Exercise ignore rules in a temporary repository, never initialize the project repo."""

import shutil
import subprocess
from pathlib import Path


def test_publication_ignores_private_files_but_keeps_templates(tmp_path):
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    shutil.copyfile(root / ".gitignore", tmp_path / ".gitignore")
    private = [
        ".env",
        ".env.production",
        "service.env",
        "server.key",
        "server.pem",
        "data/synaxis.sqlite3",
        "data/calls.sqlite3-wal",
        "data/syllabus/models/model_optimized.onnx",
        "data/syllabus/data/chunks.jsonl",
        "archive/pre-v040/manifest.json",
        "test-results/log.json",
        "deploy-backups/db.sqlite3",
        "rootfs/opt/python/bin/python3.12",
        ".venv/bin/python",
    ]
    public = [
        "main.py",
        ".env.example",
        "config/models/providers.json",
        "config/runtime/network.json",
        "docs/deployment.md",
        "tests/test_v040_providers.py",
        "admin/admin.js",
        "uv.lock",
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
