import json
import shutil
import sqlite3

import pytest
from jwt import InvalidTokenError
from pydantic import ValidationError

from auth.admin_security import password_hash, set_password
from auth.security import decode_token, load_jwt_secret, token_for, verify_password
from core.runtime_config import CONFIG_ROOT, NetworkConfig, runtime_values
from core.settings import Settings
from domain.errors import HarnessError
from domain.schemas import TutorRequest
from llm.contracts import CallContext
from runtime.maintenance import InstanceLock, reset_databases
from storage.artifacts import ArtifactStore
from storage.database import Database, now
from storage.retention import expire_artifacts


def test_json_defaults_environment_priority_and_validation(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    shutil.copytree(CONFIG_ROOT, directory)
    network = directory / "runtime/network.json"
    value = json.loads(network.read_text())
    value["port"] = 6767
    network.write_text(json.dumps(value))
    assert runtime_values(directory)["port"] == 6767
    assert Settings(_env_file=None, config_dir=directory).port == 6767
    monkeypatch.setenv("PORT", "7878")
    assert Settings(_env_file=None, config_dir=directory).port == 7878
    assert Settings(_env_file=None, config_dir=directory, port=8989).port == 8989
    value["private_key"] = "must-not-be-json"
    network.write_text(json.dumps(value))
    with pytest.raises(ValidationError):
        runtime_values(directory)


@pytest.mark.parametrize(
    "origin",
    ["*", "https://*.example", "https://*", "https://x/", "https://x/path", "https://user:pass@x", "file:///tmp", "https://x?secret=yes"],
)
def test_cors_rejects_wildcards_and_credential_urls(origin):
    with pytest.raises(ValueError):
        NetworkConfig(cors_origins=[origin])


def test_admin_password_console_and_no_default(settings):
    assert password_hash(settings) == ""
    answers = iter(["synthetic-admin-password"] * 2)
    path = set_password(settings, prompt=lambda _: next(answers))
    assert path.name == "admin-password.hash"
    assert verify_password(password_hash(settings), "synthetic-admin-password")
    with pytest.raises(ValueError):
        set_password(settings, prompt=lambda q: "a" if "Repeat" in q else "b")
    assert verify_password(password_hash(settings), "synthetic-admin-password")


def test_double_confirmation_backup_preserves_files_and_invalidates_jwt(settings):
    db = Database(settings.db_path)
    db.initialize()
    artifact = settings.data_dir / "artifacts"
    artifact.mkdir()
    (artifact / "keep.log").write_text("preserved")
    key = settings.data_dir / "jwt.key"
    key.write_text("existing-private-key-at-least-thirty-two-characters")
    before = load_jwt_secret(settings)
    token = token_for({"id": 1, "token_version": 0}, before, 60)
    assert reset_databases(settings, confirm=lambda _: "NO") is None
    assert settings.db_path.exists()
    answers = iter(["RESET DATABASES", "wrong directory"])
    assert reset_databases(settings, confirm=lambda _: next(answers)) is None
    answers = iter(["RESET DATABASES", str(settings.data_dir.resolve())])
    with InstanceLock(settings.data_dir):
        backup = reset_databases(settings, confirm=lambda _: next(answers))
    assert not settings.db_path.exists() and not db.ledger.exists()
    for name in ("synaxis.sqlite3", "calls.sqlite3"):
        with sqlite3.connect(backup / name) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert (artifact / "keep.log").read_text() == "preserved"
    assert key.read_text() == "existing-private-key-at-least-thirty-two-characters"
    assert load_jwt_secret(settings) != before
    with pytest.raises(InvalidTokenError):
        decode_token(token, load_jwt_secret(settings))


def test_backup_failure_leaves_both_databases(settings, monkeypatch):
    db = Database(settings.db_path)
    db.initialize()
    answers = iter(["RESET DATABASES", str(settings.data_dir.resolve())])
    monkeypatch.setattr(
        "runtime.maintenance.sqlite3.connect",
        lambda _: (_ for _ in ()).throw(OSError("synthetic backup failure")),
    )
    with pytest.raises(OSError):
        reset_databases(settings, confirm=lambda _: next(answers))
    assert settings.db_path.exists() and db.ledger.exists()


def test_artifact_expiration_keeps_active_sessions_and_accounting(settings):
    from auth.security import hash_password

    db = Database(settings.db_path)
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'test',?,?)",
            (hash_password("synthetic-password"), now()),
        )
    sid = db.create_session(1, TutorRequest(message="test").model_dump())
    store, ctx = ArtifactStore(settings, db), CallContext(1, sid, "expert")
    log = store.write(ctx, "round.log", "text/plain", b"thinking")
    image = store.write(ctx, "render-output.png", "image/png", b"synthetic")
    with db.connection() as conn:
        conn.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00+00:00'")
    assert expire_artifacts(settings, db) == 0
    db.save_session(sid, "completed", db.session(sid)["state"])
    budget = db.budget_snapshot(sid, 1, settings)
    assert expire_artifacts(settings, db) == 2
    assert not (store.root / image["artifact_id"]).exists()
    with pytest.raises(HarnessError, match="ARTIFACT_EXPIRED"):
        store.read(ctx, log["artifact_id"])
    assert db.session(sid) and db.budget_snapshot(sid, 1, settings) == budget
    assert expire_artifacts(settings, db) == 0
