import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from domain.errors import HarnessError
from llm.contracts import CallContext
from llm.deepseek import DeepSeekClient
from storage.budget import cny_cost
from storage.client_store import StoreError
from storage.database import Database, now


def test_legacy_migration_backfills_only_public_data_once(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    presentation = {"version": "1.0", "blocks": [{"type": "markdown", "text": "public"}]}
    state = {
        "request": {"message": "legacy question", "subject": "math", "images": []},
        "plan": None,
        "question": None,
        "presentations": [presentation],
        "error": None,
        "steps": 1,
        "tool_calls": 0,
        "messages": [{"role": "assistant", "content": [{"type": "thinking", "thinking": "SECRET"}]}],
    }
    with sqlite3.connect(settings.db_path) as c:
        c.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,active INTEGER DEFAULT 1,token_version INTEGER DEFAULT 0,created_at TEXT);CREATE TABLE sessions(id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,state TEXT,created_at TEXT,updated_at TEXT);CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,kind TEXT,data TEXT,created_at TEXT);"
        )
        c.execute("INSERT INTO users VALUES(1,'old','preserved-hash',1,0,?)", (now(),))
        c.execute("INSERT INTO sessions VALUES('s',1,'completed',?,?,?)", (json.dumps(state), now(), now()))
        c.execute(
            "INSERT INTO events(session_id,kind,data,created_at) VALUES('s','presentation',?,?)",
            (json.dumps(presentation), now()),
        )
    db = Database(settings.db_path)
    db.initialize()
    first = db.messages_page("s", 0, 100)
    db.initialize()
    assert db.messages_page("s", 0, 100) == first
    assert [x["role"] for x in first["items"]] == ["student", "assistant"]
    assert "SECRET" not in json.dumps(first) and db.user(username="old")["password_hash"] == "preserved-hash"
    assert (
        db.list_sessions(1, 20, None, "math", None, None, "all", None)["items"][0]["title"]
        == "legacy question"
    )


def test_atomic_budget_reservation_across_connections_recovery_retention(settings):
    first, second = Database(settings.db_path), Database(settings.db_path)
    first.initialize()

    def reserve(db):
        try:
            return db.begin_call(
                1,
                "s",
                "expert",
                "deepseek-flash",
                {},
                currency="CNY",
                reservation=Decimal(3),
                settings=settings,
            )
        except StoreError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, [first, second]))
    assert sum(type(x) is int for x in results) == 1 and "BUDGET_EXCEEDED" in results
    snapshot = first.budget_snapshot("s", 1, settings)
    assert Decimal(snapshot["reserved"]) == 3 and Decimal(snapshot["available"]) == 2
    first.recover()
    snapshot = first.budget_snapshot("s", 1, settings)
    assert Decimal(snapshot["reserved"]) == 0 and Decimal(snapshot["uncertain"]) == 3
    with first.connection(True) as c:
        c.execute("UPDATE calls SET created_at=?", ((datetime.now(UTC) - timedelta(days=366)).isoformat(),))
    first.compact_ledger(365, 65536)
    assert first.usage(1)["items"] == []
    assert Decimal(first.budget_snapshot("s", 1, settings)["available"]) == 2


def test_cny_prices_are_official_peak_and_legacy_costs_not_converted(settings):
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 10,
        "output_tokens": 50,
    }
    assert cny_cost(settings, "deepseek-flash", usage) == Decimal("0.000660000")
    settings.pricing_mode = "off_peak"
    assert cny_cost(settings, "deepseek-flash", usage) == Decimal("0.000660000")
    db = Database(settings.db_path)
    db.initialize()
    cid = db.begin_call(1, "s", "expert", "deepseek-flash", {}, currency="USD")
    db.finish_call(cid, usage=usage, cost="0.000099")
    budget = db.budget_snapshot("s", 1, settings)
    assert Decimal(budget["spent"]) == Decimal("0.000660000")
    assert db.usage(1)["items"][0]["currency"] == "USD"


async def test_budget_blocks_paid_transport_and_releases_known_spend(settings):
    db = Database(settings.db_path)
    db.initialize()
    cid = db.begin_call(
        1, "s", "image", "image", {}, currency="CNY", reservation=Decimal("4.999"), settings=settings
    )
    db.finish_call(cid, cost=Decimal("4.999"))
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        llm = DeepSeekClient(settings, db, http)
        with pytest.raises(HarnessError, match="BUDGET_EXCEEDED"):
            await llm.complete(
                CallContext(1, "s", "expert"),
                system="x",
                messages=[{"role": "user", "content": "x"}],
                model="deepseek-flash",
            )
        assert not requests
        await llm.complete(
            CallContext(1, "other", "expert"),
            system="x",
            messages=[{"role": "user", "content": "x"}],
            model="deepseek-flash",
        )
    budget = db.budget_snapshot("other", 1, settings)
    assert Decimal(budget["reserved"]) == 0 and Decimal(budget["spent"]) == Decimal("0.000180000")
    assert db.usage(1)["items"][0]["currency"] == "CNY"


async def test_ambiguous_failure_retains_reservation_and_unknown_model_fails_closed(settings):
    db = Database(settings.db_path)
    db.initialize()
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("unknown billing result")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        llm = DeepSeekClient(settings, db, http)
        with pytest.raises(HarnessError, match="PROVIDER_TRANSPORT_ERROR"):
            await llm.complete(
                CallContext(1, "s", "search"),
                system="x",
                messages=[{"role": "user", "content": "x"}],
                model="deepseek-flash",
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            )
        budget = db.budget_snapshot("s", 1, settings)
        assert Decimal(budget["uncertain"]) > 4 and Decimal(budget["reserved"]) == 0
        with pytest.raises(HarnessError, match="BUDGET_EXCEEDED"):
            await llm.complete(
                CallContext(1, "s", "search"),
                system="x",
                messages=[{"role": "user", "content": "x"}],
                model="deepseek-flash",
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
            )
        with pytest.raises(HarnessError, match="BUDGET_PRICE_UNKNOWN"):
            await llm.complete(
                CallContext(1, "unknown", "expert"),
                system="x",
                messages=[{"role": "user", "content": "x"}],
                model="unknown",
            )
    assert len(requests) == 1
