import uuid
from pathlib import Path

from core.settings import Settings
from domain.errors import HarnessError
from llm.contracts import CallContext
from storage.database import Database


class ArtifactStore:
    def __init__(self, settings: Settings, db: Database):
        self.settings, self.db = settings, db
        self.root = settings.data_dir / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def write(self, ctx: CallContext, name: str, mime: str, data: bytes):
        if len(data) > self.settings.artifact_bytes:
            raise HarnessError("ARTIFACT_TOO_LARGE")
        artifact_id = uuid.uuid4().hex
        path = self.root / artifact_id
        path.write_bytes(data)
        try:
            self.db.add_artifact(artifact_id, ctx.session_id, ctx.user_id, Path(name).name, mime, len(data))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"artifact_id": artifact_id, "name": Path(name).name, "mime": mime, "bytes": len(data)}

    def read(self, ctx, artifact_id, render_only=False):
        row = self.db.artifact(artifact_id, ctx.user_id, ctx.session_id)
        if not row or (render_only and not row["name"].startswith("render-")):
            raise HarnessError("ARTIFACT_NOT_FOUND")
        if row.get("expired_at"):
            raise HarnessError("ARTIFACT_EXPIRED")
        try:
            return (self.root / row["id"]).read_bytes()
        except FileNotFoundError as exc:
            raise HarnessError("ARTIFACT_EXPIRED") from exc
