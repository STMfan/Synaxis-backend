"""Account management preserves stable IDs and accounting history."""

import sqlite3
import uuid

from auth.security import DUMMY_HASH
from storage.client_store import StoreError
from storage.database import now


def public_user(row):
    return {k: row[k] for k in ("id", "username", "active", "created_at")}


def list_users(db, query="", after=0, limit=50):
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id,username,active,created_at FROM users WHERE deleted_at IS NULL AND id>? "
            "AND instr(lower(username),lower(?))>0 ORDER BY id LIMIT ?",
            (after, query, limit + 1),
        ).fetchall()
    return {
        "items": [public_user(r) for r in rows[:limit]],
        "next_after": rows[limit - 1]["id"] if len(rows) > limit else None,
    }


def create_user(db, username, encoded):
    try:
        with db.connection() as conn:
            cursor = conn.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (username, encoded, now()),
            )
            row = conn.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
            return public_user(row)
    except sqlite3.IntegrityError as exc:
        raise StoreError("USERNAME_EXISTS", 409) from exc


def update_user(db, user_id, fields, *, delete=False):
    try:
        with db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)).fetchone()
            if not row:
                raise StoreError("NOT_FOUND", 404)
            if delete or fields.get("active") is False:
                if conn.execute(
                    "SELECT 1 FROM sessions WHERE user_id=? AND status IN ('queued','running') AND deleted_at IS NULL",
                    (user_id,),
                ).fetchone():
                    raise StoreError("USER_BUSY", 409)
            if delete:
                fields = {
                    "username": "deleted-" + str(user_id) + "-" + uuid.uuid4().hex,
                    "password_hash": DUMMY_HASH,
                    "active": False,
                    "deleted_at": now(),
                }
            if not fields:
                return public_user(row)
            if set(fields) - {"username", "password_hash", "active", "deleted_at"}:
                raise ValueError("Unsupported account field")
            assignments = ",".join(f"{key}=?" for key in fields)
            conn.execute(
                f"UPDATE users SET {assignments},token_version=token_version+1 WHERE id=?",
                [*fields.values(), user_id],
            )
            conn.execute("DELETE FROM refresh_tokens WHERE user_id=?", (user_id,))
            return public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    except sqlite3.IntegrityError as exc:
        raise StoreError("USERNAME_EXISTS", 409) from exc
