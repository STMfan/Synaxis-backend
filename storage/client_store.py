import base64
import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from domain.views import session_summary, session_view


def timestamp():
    return datetime.now(UTC).isoformat()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def pack_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def unpack_cursor(value, scope):
    try:
        data = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.get("scope") != scope:
            raise ValueError()
        return data
    except (ValueError, TypeError, UnicodeError) as exc:
        raise StoreError("INVALID_CURSOR", 400) from exc


class StoreError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def record_message(c, sid, role, kind, data, images=None, created_at=None, inferred=False, emit=True):
    when = created_at or timestamp()
    mid = c.execute(
        "INSERT INTO public_messages(session_id,role,kind,data,images,created_at,time_accuracy) VALUES(?,?,?,?,?,?,?)",
        (
            sid,
            role,
            kind,
            json.dumps(data, ensure_ascii=False),
            json.dumps(images or []),
            when,
            "inferred" if inferred else "exact",
        ),
    ).lastrowid
    message = public_message(c.execute("SELECT * FROM public_messages WHERE id=?", (mid,)).fetchone())
    if emit:
        insert_event(c, sid, "message", {"message": message}, when)
    return mid


def insert_event(c, sid, kind, data, when=None):
    return c.execute(
        "INSERT INTO events(session_id,kind,data,created_at) VALUES(?,?,?,?)",
        (sid, kind, json.dumps(data, ensure_ascii=False), when or timestamp()),
    ).lastrowid


def public_message(row):
    data = json.loads(row["data"])
    images = json.loads(row["images"])
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "role": row["role"],
        "kind": row["kind"],
        "text": data.get("text"),
        "question_id": data.get("question_id"),
        "presentation": data.get("presentation"),
        "created_at": row["created_at"],
        "time_accuracy": row["time_accuracy"],
        "images": [
            {
                "index": i,
                "media_type": image["media_type"],
                "bytes": len(base64.b64decode(image["data"])),
                "url": f"/v1/sessions/{row['session_id']}/messages/{row['id']}/images/{i}",
            }
            for i, image in enumerate(images)
        ],
    }


def migrate_client(c):
    """Idempotent in-place migration; existing auth, artifacts and event IDs survive."""
    columns = {r["name"] for r in c.execute("PRAGMA table_info(sessions)")}
    for name, definition in {
        "title": "TEXT NOT NULL DEFAULT ''",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "subject": "TEXT",
        "request_meta": "TEXT NOT NULL DEFAULT '{}'",
        "image_count": "INTEGER NOT NULL DEFAULT 0",
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        "archived": "INTEGER NOT NULL DEFAULT 0",
        "deleted_at": "TEXT",
    }.items():
        if name not in columns:
            c.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
    if "preferences" not in {r["name"] for r in c.execute("PRAGMA table_info(users)")}:
        c.execute("ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")
    c.executescript("""
    CREATE TABLE IF NOT EXISTS public_messages (
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
      role TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, images TEXT NOT NULL DEFAULT '[]',
      created_at TEXT NOT NULL, time_accuracy TEXT NOT NULL DEFAULT 'exact'
    );
    CREATE INDEX IF NOT EXISTS messages_session ON public_messages(session_id,id);
    CREATE INDEX IF NOT EXISTS sessions_directory ON sessions(user_id,deleted_at,archived,pinned,updated_at,id);
    CREATE TABLE IF NOT EXISTS submissions (
      user_id INTEGER NOT NULL REFERENCES users(id), operation TEXT NOT NULL, key TEXT NOT NULL,
      fingerprint TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES sessions(id), response TEXT NOT NULL,
      created_at TEXT NOT NULL, PRIMARY KEY(user_id,operation,key)
    );
    CREATE TABLE IF NOT EXISTS refresh_tokens (
      hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), family TEXT NOT NULL,
      token_version INTEGER NOT NULL, expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS refresh_owner ON refresh_tokens(user_id);
    CREATE TABLE IF NOT EXISTS client_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    """)
    c.execute("BEGIN IMMEDIATE")
    if c.execute("SELECT 1 FROM client_migrations WHERE version=2").fetchone():
        return
    for row in c.execute("SELECT * FROM sessions").fetchall():
        state = json.loads(row["state"])
        request = state["request"]
        text = request.get("message", "")
        meta = {k: v for k, v in request.items() if k not in ("message", "images")}
        c.execute(
            "UPDATE sessions SET title=?,summary=?,subject=?,request_meta=?,image_count=? WHERE id=?",
            (
                text[:100],
                text[:500],
                request.get("subject"),
                json.dumps(meta, ensure_ascii=False),
                len(request.get("images", [])),
                row["id"],
            ),
        )
        record_message(
            c,
            row["id"],
            "student",
            "question",
            {"text": text},
            request.get("images"),
            row["created_at"],
            emit=False,
        )
        answers = []
        for message in state.get("messages", []):
            if message.get("role") != "user":
                continue
            for block in message.get("content", []):
                if block.get("type") == "text":
                    try:
                        value = json.loads(block["text"])
                    except (ValueError, KeyError):
                        continue
                    if isinstance(value, dict) and "answer" in value and "question_id" in value:
                        answers.append(value)
        pending = False
        for event in c.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY id", (row["id"],)
        ).fetchall():
            if event["kind"] == "presentation":
                record_message(
                    c,
                    row["id"],
                    "assistant",
                    "presentation",
                    {"presentation": json.loads(event["data"])},
                    created_at=event["created_at"],
                    emit=False,
                )
            elif event["kind"] == "waiting":
                pending = True
            elif event["kind"] == "running" and pending and answers:
                answer = answers.pop(0)
                record_message(
                    c,
                    row["id"],
                    "student",
                    "answer",
                    {"text": answer["answer"], "question_id": answer["question_id"]},
                    created_at=event["created_at"],
                    inferred=True,
                    emit=False,
                )
                pending = False
        for answer in answers:
            record_message(
                c,
                row["id"],
                "student",
                "answer",
                {"text": answer["answer"], "question_id": answer["question_id"]},
                created_at=row["updated_at"],
                inferred=True,
                emit=False,
            )
    c.execute("INSERT INTO client_migrations VALUES(2,?)", (timestamp(),))


class ClientStore:
    def cleanup_refresh_tokens(self):
        with self.connection() as c:
            return c.execute("DELETE FROM refresh_tokens WHERE expires_at<?", (timestamp(),)).rowcount

    def raw_session(self, c, sid, uid=None, include_deleted=False):
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if (
            not row
            or (uid is not None and row["user_id"] != uid)
            or (row["deleted_at"] and not include_deleted)
        ):
            raise StoreError("NOT_FOUND", 404)
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def insert_session(self, c, uid, request):
        sid, when = uuid.uuid4().hex, timestamp()
        state = {
            "request": request,
            "plan": None,
            "messages": [],
            "presentations": [],
            "question": None,
            "error": None,
            "steps": 0,
            "tool_calls": 0,
        }
        text = request.get("message", "")
        meta = {k: v for k, v in request.items() if k not in ("message", "images")}
        c.execute(
            "INSERT INTO sessions(id,user_id,status,state,created_at,updated_at,title,summary,subject,request_meta,image_count) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid,
                uid,
                "queued",
                json.dumps(state, ensure_ascii=False),
                when,
                when,
                text[:100],
                text[:500],
                request.get("subject"),
                json.dumps(meta, ensure_ascii=False),
                len(request.get("images", [])),
            ),
        )
        mid = record_message(c, sid, "student", "question", {"text": text}, request.get("images"), when)
        insert_event(c, sid, "queued", {})
        return sid, mid

    def submit(self, uid, operation, key, payload, mutate):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            digest = fingerprint(payload)
            if key:
                receipt = c.execute(
                    "SELECT * FROM submissions WHERE user_id=? AND operation=? AND key=?",
                    (uid, operation, key),
                ).fetchone()
                if receipt:
                    if receipt["fingerprint"] != digest:
                        raise StoreError("IDEMPOTENCY_CONFLICT")
                    row = self.raw_session(c, receipt["session_id"], uid, include_deleted=True)
                    if row["deleted_at"]:
                        raise StoreError("SESSION_DELETED", 410)
                    return json.loads(receipt["response"]), True
            sid, mid = mutate(c)
            result = {**session_view(self.raw_session(c, sid, uid)), "submission_message_id": mid}
            if key:
                c.execute(
                    "INSERT INTO submissions VALUES(?,?,?,?,?,?,?)",
                    (uid, operation, key, digest, sid, json.dumps(result, ensure_ascii=False), timestamp()),
                )
            return result, False

    def append_input(self, c, sid, uid, body, answer=False):
        row = self.raw_session(c, sid, uid)
        if row["archived"]:
            raise StoreError("SESSION_ARCHIVED")
        state = row["state"]
        if answer:
            if (
                row["status"] != "waiting"
                or not state["question"]
                or state["question"]["id"] != body["question_id"]
            ):
                raise StoreError("QUESTION_NOT_PENDING")
            state["messages"].append(
                {"role": "user", "content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}]}
            )
            kind, data, images = "answer", {"text": body["answer"], "question_id": body["question_id"]}, []
            state["question"] = None
        else:
            if row["status"] not in ("completed", "failed", "cancelled"):
                raise StoreError(
                    "SESSION_BUSY" if row["status"] in ("queued", "running") else "ANSWER_REQUIRED"
                )
            # Complete orphaned tool requests left by interruption before adding a new turn.
            history = state["messages"]
            if history and history[-1].get("role") == "assistant":
                missing = [b for b in history[-1]["content"] if b.get("type") == "tool_use"]
                ids = [b.get("id") for b in missing]
                if any(
                    not isinstance(b.get("id"), str)
                    or not isinstance(b.get("name"), str)
                    or not isinstance(b.get("input"), dict)
                    for b in missing
                ) or len(set(ids)) != len(ids):
                    # Invalid provider blocks were never dispatched; do not forward
                    # their broken protocol into the next request.
                    history[-1]["content"] = [
                        b for b in history[-1]["content"] if b.get("type") != "tool_use"
                    ]
                    if not history[-1]["content"]:
                        history.pop()
                    missing = []
                if missing:
                    history.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": b["id"],
                                    "content": '{"error":"INTERRUPTED"}',
                                }
                                for b in missing
                            ],
                        }
                    )
            content = [
                {"type": "text", "text": body["message"]},
                *[{"type": "image", "source": {"type": "base64", **i}} for i in body["images"]],
            ]
            history.append({"role": "user", "content": content})
            if state["plan"] is None:
                state["pending_input"] = {
                    **state["request"],
                    "message": (
                        "原始题目："
                        + state["request"].get("message", "")[:6000]
                        + "\n当前追问："
                        + body["message"]
                    )[:20000],
                    "images": body["images"] or state["request"].get("images", []),
                }
            state["steps"], state["tool_calls"], state["error"], state["question"] = 0, 0, None, None
            state["request"]["allow_paid_images"] = body["allow_paid_images"]
            state["request"]["allow_max_thinking"] = body.get("allow_max_thinking", True)
            kind, data, images = "follow_up", {"text": body["message"]}, body["images"]
        # Max is optional per student turn; continuation never inherits it silently.
        state.pop("effort", None)
        state.pop("max_thinking_used", None)
        c.execute(
            "UPDATE sessions SET status='queued',state=?,updated_at=? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), timestamp(), sid),
        )
        mid = record_message(c, sid, "student", kind, data, images)
        insert_event(c, sid, "queued", {})
        return sid, mid

    def list_sessions(self, uid, limit, cursor, subject, status, q, archived, pinned):
        scope = fingerprint([uid, subject, status, q, archived, pinned])
        where, params = ["user_id=?", "deleted_at IS NULL"], [uid]
        for field, value in (("subject", subject), ("status", status), ("pinned", pinned)):
            if value is not None:
                where.append(f"{field}=?")
                params.append(value)
        if archived != "all":
            where.append("archived=?")
            params.append(archived == "only")
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(
                "(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM public_messages p WHERE p.session_id=sessions.id AND json_extract(p.data,'$.text') LIKE ? ESCAPE '\\'))"
            )
            params.extend(["%" + escaped + "%"] * 3)
        if cursor:
            value = unpack_cursor(cursor, scope)
            values = value.get("position")
            if (
                not isinstance(values, list)
                or len(values) != 3
                or type(values[0]) is not int
                or values[0] not in (0, 1)
                or not all(isinstance(x, str) for x in values[1:])
            ):
                raise StoreError("INVALID_CURSOR", 400)
            where.append("(pinned,updated_at,id)<(?,?,?)")
            params.extend(values)
        with self.connection() as c:
            rows = c.execute(
                f"SELECT id,title,summary,subject,request_meta,image_count,status,json_extract(state,'$.plan.expert') expert,created_at,updated_at,pinned,archived FROM sessions WHERE {' AND '.join(where)} ORDER BY pinned DESC,updated_at DESC,id DESC LIMIT ?",
                [*params, limit + 1],
            ).fetchall()
        items = [session_summary(dict(row)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = pack_cursor(
                {"scope": scope, "position": [last["pinned"], last["updated_at"], last["id"]]}
            )
        return {"items": items, "next_cursor": next_cursor}

    def messages_page(self, sid, after, limit):
        with self.connection() as c:
            rows = c.execute(
                "SELECT * FROM public_messages WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                (sid, after, limit + 1),
            ).fetchall()
        items = [public_message(row) for row in rows[:limit]]
        return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}

    def events_page(self, sid, after, limit):
        with self.connection() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE session_id=? AND id>? ORDER BY id LIMIT ?",
                (sid, after, limit + 1),
            ).fetchall()
        items = [
            {
                "id": row["id"],
                "event": row["kind"],
                "data": json.loads(row["data"]),
                "created_at": row["created_at"],
            }
            for row in rows[:limit]
        ]
        return {"items": items, "next_after": items[-1]["id"] if len(rows) > limit else None}

    def commit_presentation(self, sid, state, result, warning=None):
        with self.connection() as c:
            c.execute(
                "UPDATE sessions SET status='running',state=?,updated_at=? WHERE id=? AND deleted_at IS NULL",
                (json.dumps(state, ensure_ascii=False), timestamp(), sid),
            )
            record_message(c, sid, "assistant", "presentation", {"presentation": result})
            if warning:
                insert_event(c, sid, "warning", {"code": warning, "fallback": "markdown"})
            insert_event(c, sid, "presentation", result)

    def patch_session(self, sid, uid, values):
        with self.connection() as c:
            self.raw_session(c, sid, uid)
            for field, value in values.items():
                if value is not None:
                    c.execute(
                        f"UPDATE sessions SET {field}=?,updated_at=? WHERE id=?", (value, timestamp(), sid)
                    )
            return self.raw_session(c, sid, uid)

    def delete_session(self, sid, uid):
        with self.connection() as c:
            self.raw_session(c, sid, uid, include_deleted=True)
            c.execute(
                "UPDATE sessions SET deleted_at=COALESCE(deleted_at,?),updated_at=? WHERE id=?",
                (timestamp(), timestamp(), sid),
            )

    def set_preferences(self, uid, values):
        with self.connection() as c:
            c.execute(
                "UPDATE users SET preferences=? WHERE id=?", (json.dumps(values, ensure_ascii=False), uid)
            )

    def issue_refresh(self, c, user, days, family=None):
        token = secrets.token_urlsafe(48)
        c.execute(
            "INSERT INTO refresh_tokens VALUES(?,?,?,?,?,0)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                user["id"],
                family or uuid.uuid4().hex,
                user["token_version"],
                (datetime.now(UTC) + timedelta(days=days)).isoformat(),
            ),
        )
        return token

    def rotate_refresh(self, token, days):
        failure = None
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM refresh_tokens WHERE hash=?", (hashlib.sha256(token.encode()).hexdigest(),)
            ).fetchone()
            user = c.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone() if row else None
            if (
                not user
                or not user["active"]
                or row["expires_at"] <= timestamp()
                or row["token_version"] != user["token_version"]
            ):
                failure = "UNAUTHORIZED"
            elif row["used"]:
                # Persist revocation before returning an error; never rollback this change.
                c.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (user["id"],))
                c.execute("DELETE FROM refresh_tokens WHERE user_id=?", (user["id"],))
                failure = "REFRESH_REUSED"
            else:
                c.execute("UPDATE refresh_tokens SET used=1 WHERE hash=?", (row["hash"],))
                result = (dict(user), self.issue_refresh(c, user, days, row["family"]))
        if failure:
            raise StoreError(failure, 401)
        return result

    def revoke_auth(self, uid, old_hash=None, new_hash=None):
        with self.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            if new_hash:
                changed = c.execute(
                    "UPDATE users SET password_hash=?,token_version=token_version+1 WHERE id=? AND password_hash=?",
                    (new_hash, uid, old_hash),
                ).rowcount
                if not changed:
                    raise StoreError("PASSWORD_CHANGED", 409)
            else:
                c.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (uid,))
            c.execute("DELETE FROM refresh_tokens WHERE user_id=?", (uid,))

    def usage_summary(self, uid, group_by, date_from, date_to, session_id, limit, cursor):
        group = {"day": "substr(created_at,1,10)", "session": "COALESCE(session_id,'')", "total": "'total'"}[
            group_by
        ]
        scope = fingerprint([uid, group_by, date_from, date_to, session_id])
        end = (datetime.fromisoformat(date_to).date() + timedelta(days=1)).isoformat()
        where, args = ["user_id=?", "created_at>=?", "created_at<?"], [uid, date_from, end]
        if session_id:
            where.append("session_id=?")
            args.append(session_id)
        having, having_args = "", []
        if cursor:
            value = unpack_cursor(cursor, scope).get("position")
            if not isinstance(value, list) or len(value) != 2 or not all(isinstance(x, str) for x in value):
                raise StoreError("INVALID_CURSOR", 400)
            having, having_args = "HAVING (bucket,currency)<(?,?)", value
        with self.connection(True) as c:
            rows = c.execute(
                f"SELECT {group} bucket,currency,COUNT(*) calls,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(cache_read_tokens) cache_read_tokens FROM calls WHERE {' AND '.join(where)} GROUP BY bucket,currency {having} ORDER BY bucket DESC,currency DESC LIMIT ?",
                [*args, *having_args, limit + 1],
            ).fetchall()
            items = []
            for row in rows[:limit]:
                cost, unknown = Decimal(0), 0
                for call in c.execute(
                    f"SELECT cost FROM calls WHERE {' AND '.join(where)} AND {group}=? AND currency=?",
                    [*args, row["bucket"], row["currency"]],
                ):
                    if call["cost"] is None:
                        unknown += 1
                    else:
                        cost += Decimal(call["cost"])
                items.append(
                    {
                        "key": row["bucket"],
                        **{
                            k: row[k]
                            for k in (
                                "currency",
                                "calls",
                                "input_tokens",
                                "output_tokens",
                                "cache_read_tokens",
                            )
                        },
                        "estimated_cost": str(cost),
                        "unknown_cost_calls": unknown,
                    }
                )
        next_cursor = (
            pack_cursor(
                {"scope": scope, "position": [rows[limit - 1]["bucket"], rows[limit - 1]["currency"]]}
            )
            if len(rows) > limit
            else None
        )
        return {
            "group_by": group_by,
            "date_from": date_from,
            "date_to": date_to,
            "items": items,
            "next_cursor": next_cursor,
        }
