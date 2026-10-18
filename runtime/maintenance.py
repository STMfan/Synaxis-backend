import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


class InstanceLock:
    """Keep an exclusive lock in the data directory for the entire launcher lifetime."""

    def __init__(self, directory):
        self.path = Path(directory) / "launcher.lock"
        self.stream = None

    def __enter__(self):
        import os

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("Launcher lock must not be a symlink")
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise RuntimeError("Another managed instance is using this data directory") from exc
        return self

    def __exit__(self, *args):
        self.stream.close()


def reset_databases(settings, confirm=None):
    """Offline operation: two exact confirmations and complete backups before any unlink."""
    confirm = confirm or input
    directory = settings.data_dir.resolve()
    targets = [directory / "synaxis.sqlite3", directory / "calls.sqlite3"]
    for target in targets:
        for path in (target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
            if path.is_symlink():
                raise ValueError("Initialization refuses database symlinks")
    print(
        f"Reset users, conversations and accounting in {directory}. Attachments, logs and keys are retained."
    )
    if confirm("First confirmation / 第一次确认：输入 RESET DATABASES: ") != "RESET DATABASES":
        return None
    if confirm(f"Second confirmation / 第二次确认：输入完整目录 {directory}: ") != str(directory):
        return None
    backup = (
        directory / "backups" / (datetime.now(UTC).strftime("reset-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    )
    backup.mkdir(parents=True, mode=0o700)
    manifest = {"scope": "two-databases-only", "files": []}
    # No database is removed if either backup fails.
    for path in targets:
        if path.exists():
            destination = backup / path.name
            with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(destination)) as copy:
                source.backup(copy)
                if copy.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity check failed")
                copy.commit()
            destination.chmod(0o600)
            manifest["files"].append(
                {"name": path.name, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
            )
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    # Keep key files, but invalidate old tokens before IDs can be reallocated.
    generation = directory / "jwt-generation"
    generation.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    generation.chmod(0o600)
    for path in targets:
        path.unlink(missing_ok=True)
        Path(str(path) + "-wal").unlink(missing_ok=True)
        Path(str(path) + "-shm").unlink(missing_ok=True)
    return backup
