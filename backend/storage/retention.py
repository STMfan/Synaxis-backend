"""Expire recorded artifacts, preserving sessions and cumulative budgets."""

import re
from datetime import UTC, datetime, timedelta

from storage.database import now


def expire_artifacts(settings, db):
    clock = datetime.now(UTC)
    regular = (clock - timedelta(days=settings.artifact_retention_days)).isoformat()
    logs = (clock - timedelta(days=settings.log_retention_days)).isoformat()
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT a.id FROM artifacts a JOIN sessions s ON s.id=a.session_id "
            "WHERE a.expired_at IS NULL AND s.status NOT IN ('queued','running') AND "
            "((a.mime='text/plain' AND a.created_at<?) OR (a.mime!='text/plain' AND a.created_at<?))",
            (logs, regular),
        ).fetchall()
    root = settings.data_dir / "artifacts"
    count = 0
    for row in rows:
        if not re.fullmatch(r"[0-9a-f]{32}", row["id"]):
            continue
        with db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM artifacts a JOIN sessions s ON s.id=a.session_id "
               "WHERE a.id=? AND a.expired_at IS NULL AND s.status NOT IN ('queued','running')", (row["id"],)).fetchone() is None:
                continue
            (root / row["id"]).unlink(missing_ok=True)
            conn.execute("UPDATE artifacts SET expired_at=? WHERE id=?", (now(), row["id"]))
        count += 1
    return count
