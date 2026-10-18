import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from storage.budget import BudgetStore, budget_tables
from storage.client_store import ClientStore, migrate_client


def now() -> str:
    return datetime.now(UTC).isoformat()


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database(ClientStore, BudgetStore):
    """Short transactions; a separate compactable ledger never deletes users."""

    def __init__(self, path: Path):
        self.path = path
        self.ledger = path.with_name("calls.sqlite3")
        self.lock = threading.RLock()

    @contextmanager
    def connection(self, ledger=False):
        with self.lock:
            conn = sqlite3.connect(self.ledger if ledger else self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              token_version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
              kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_session ON events(session_id,id);
            CREATE TABLE IF NOT EXISTS artifacts (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
              user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, mime TEXT NOT NULL,
              bytes INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            """)
            migrate_client(c)
            if "deleted_at" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
                c.execute("ALTER TABLE users ADD COLUMN deleted_at TEXT")
            if "expired_at" not in {r["name"] for r in c.execute("PRAGMA table_info(artifacts)")}:
                c.execute("ALTER TABLE artifacts ADD COLUMN expired_at TEXT")
        with self.connection(True) as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, session_id TEXT,
              stage TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
              input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
              cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
              cost TEXT, currency TEXT NOT NULL, pricing_basis TEXT NOT NULL,
              request TEXT NOT NULL, response TEXT, error TEXT
            );
            CREATE INDEX IF NOT EXISTS calls_owner ON calls(user_id,id);
            CREATE INDEX IF NOT EXISTS calls_date ON calls(created_at);
            """)
            budget_tables(c)

    def user(self, username=None, user_id=None):
        with self.connection() as c:
            row = c.execute(
                "SELECT * FROM users WHERE username=?"
                if username is not None
                else "SELECT * FROM users WHERE id=?",
                (username if username is not None else user_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_session(self, user_id, request):
        with self.connection() as c:
            session_id, _ = self.insert_session(c, user_id, request)
        return session_id

    def session(self, session_id, user_id=None):
        with self.connection() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None or row["deleted_at"] or (user_id is not None and row["user_id"] != user_id):
            return None
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def save_session(self, session_id, status, state):
        with self.connection() as c:
            c.execute(
                "UPDATE sessions SET status=?,state=?,updated_at=?,subject=COALESCE(?,subject) WHERE id=? AND deleted_at IS NULL",
                (status, dumps(state), now(), (state.get("plan") or {}).get("subject"), session_id),
            )

    def claim_answer(self, session_id, user_id, question_id, answer):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            self.append_input(
                c, session_id, user_id, {"question_id": question_id, "answer": answer}, answer=True
            )

    def event(self, session_id, kind, data):
        with self.connection() as c:
            cursor = c.execute(
                "INSERT INTO events(session_id,kind,data,created_at) VALUES(?,?,?,?)",
                (session_id, kind, dumps(data), now()),
            )
            return cursor.lastrowid

    def events(self, session_id, after=0):
        with self.connection() as c:
            return [
                {"id": r["id"], "kind": r["kind"], "data": json.loads(r["data"])}
                for r in c.execute(
                    "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT 100",
                    (session_id, after),
                ).fetchall()
            ]

    def recover(self):
        # No automatic paid retries after a process restart.
        with self.connection() as c:
            rows = c.execute(
                "SELECT id,state FROM sessions WHERE status IN ('queued','running') AND deleted_at IS NULL"
            ).fetchall()
        for row in rows:
            state = json.loads(row["state"])
            state["error"] = "PROCESS_RESTARTED"
            self.save_session(row["id"], "failed", state)
            self.event(row["id"], "failed", {"code": "PROCESS_RESTARTED"})
        with self.connection(True) as c:
            c.execute("BEGIN IMMEDIATE")
            pending = c.execute("SELECT call_id FROM budget_reservations WHERE status='reserved'").fetchall()
            for row in pending:
                self.settle_budget(c, row["call_id"], None)
            c.execute(
                "UPDATE calls SET status='interrupted',error='PROCESS_RESTARTED' WHERE status='pending'"
            )

    def begin_call(
        self,
        user_id,
        session_id,
        stage,
        model,
        request,
        currency="USD",
        basis="unknown",
        reservation=None,
        settings=None,
    ):
        with self.connection(True) as c:
            c.execute("BEGIN IMMEDIATE")
            if reservation is not None:
                self.reserve_budget(c, session_id, user_id, reservation, settings)
            row = c.execute(
                """INSERT INTO calls(user_id,session_id,stage,model,status,created_at,
              currency,pricing_basis,request) VALUES(?,?,?,?,'pending',?,?,?,?)""",
                (user_id, session_id, stage, model, now(), currency, basis, dumps(request)),
            )
            call_id = row.lastrowid
            if reservation is not None:
                c.execute(
                    "INSERT INTO budget_reservations VALUES(?,?,?,'reserved',NULL)",
                    (call_id, session_id, str(reservation)),
                )
            return call_id

    def finish_call(self, call_id, *, response=None, error=None, status="success", usage=None, cost=None):
        usage = usage or {}
        with self.connection(True) as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """UPDATE calls SET status=?,response=?,error=?,input_tokens=?,output_tokens=?,
              cache_read_tokens=?,cache_write_tokens=?,cost=? WHERE id=?""",
                (
                    status,
                    dumps(response) if response is not None else None,
                    error,
                    usage.get("input_tokens", 0),
                    usage.get("output_tokens", 0),
                    usage.get("cache_read_input_tokens", 0),
                    usage.get("cache_creation_input_tokens", 0),
                    str(cost) if cost is not None else None,
                    call_id,
                ),
            )
            self.settle_budget(c, call_id, cost)

    def usage(self, user_id, before=None, limit=50, session_id=None):
        filters = ["user_id=?"]
        params = [user_id]
        if before is not None:
            filters.append("id<?")
            params.append(before)
        if session_id is not None:
            filters.append("session_id=?")
            params.append(session_id)
        with self.connection(True) as c:
            rows = c.execute(
                f"SELECT id,session_id,stage,model,status,created_at,input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,cost,currency,pricing_basis,error FROM calls WHERE {' AND '.join(filters)} ORDER BY id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        items = []
        for row in rows[:limit]:
            item = dict(row)
            items.append(item)
        return {"items": items, "next_before": items[-1]["id"] if len(rows) > limit else None}

    def compact_ledger(self, days, max_bytes):
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self.lock:
            with self.connection(True) as c:
                deleted = c.execute(
                    "DELETE FROM calls WHERE created_at<? AND status!='pending'", (cutoff,)
                ).rowcount
            with self.connection(True) as c:
                c.execute(
                    "DELETE FROM budget_reservations WHERE status!='reserved' AND NOT EXISTS(SELECT 1 FROM calls WHERE calls.id=budget_reservations.call_id)"
                )
                c.commit()
                c.execute("VACUUM")
            # Physical ledger size, including SQLite pages and indexes; oldest finished calls first.
            while self.ledger.stat().st_size > max_bytes:
                with self.connection(True) as c:
                    count = c.execute(
                        "DELETE FROM calls WHERE id IN (SELECT id FROM calls WHERE status!='pending' ORDER BY id LIMIT 100)"
                    ).rowcount
                deleted += count
                if not count:
                    break
                with self.connection(True) as c:
                    c.execute(
                        "DELETE FROM budget_reservations WHERE status!='reserved' AND NOT EXISTS(SELECT 1 FROM calls WHERE calls.id=budget_reservations.call_id)"
                    )
                    c.commit()
                    c.execute("VACUUM")
            return {
                "deleted": deleted,
                "bytes": self.ledger.stat().st_size,
                "over_limit": self.ledger.stat().st_size > max_bytes,
            }

    def add_artifact(self, artifact_id, session_id, user_id, name, mime, size):
        with self.connection() as c:
            c.execute(
                "INSERT INTO artifacts(id,session_id,user_id,name,mime,bytes,created_at) VALUES(?,?,?,?,?,?,?)",
                (artifact_id, session_id, user_id, name, mime, size, now()),
            )

    def artifact(self, artifact_id, user_id, session_id=None):
        with self.connection() as c:
            row = c.execute(
                "SELECT * FROM artifacts WHERE id=? AND user_id=?", (artifact_id, user_id)
            ).fetchone()
        if not row or (session_id is not None and row["session_id"] != session_id):
            return None
        return dict(row)
