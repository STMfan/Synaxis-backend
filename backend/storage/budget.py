"""Persistent CNY reservations live independently of compacted call history."""

import json
from decimal import Decimal

from core.configuration import MODELS
from storage.client_store import StoreError


def budget_tables(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS session_budgets (
      session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, limit_cny TEXT NOT NULL,
      spent_cny TEXT NOT NULL DEFAULT '0', uncertain_cny TEXT NOT NULL DEFAULT '0'
    );
    CREATE TABLE IF NOT EXISTS budget_reservations (
      call_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES session_budgets(session_id),
      amount_cny TEXT NOT NULL, status TEXT NOT NULL, charged_cny TEXT
    );
    CREATE INDEX IF NOT EXISTS budget_session ON budget_reservations(session_id,status);
    CREATE INDEX IF NOT EXISTS calls_owner_date ON calls(user_id,created_at);
    """)


def cny_cost(settings, model, usage, *, snapshot=None):
    # Historical USD migration intentionally remains a conservative peak estimate.
    # Live calls must pass their immutable request-start price snapshot.
    rates = snapshot.rates if snapshot else settings.price_rates_cny.get(model)
    if rates is None:
        return None
    hit = usage.get("cache_read_input_tokens", usage.get("cache_read_tokens", 0))
    miss = usage.get("input_tokens", 0) + usage.get(
        "cache_creation_input_tokens", usage.get("cache_write_tokens", 0)
    )
    out = usage.get("output_tokens", 0)
    return (
        (
            Decimal(hit) * Decimal(str(rates[0]))
            + Decimal(miss) * Decimal(str(rates[1]))
            + Decimal(out) * Decimal(str(rates[2]))
        )
        / 1_000_000
    ).quantize(Decimal("0.000000001"))


def reservation_cost(settings, model, body):
    # Reserve peak rates even when settling off-peak, including cross-boundary calls.
    rates = settings.price_rates_cny.get(model)
    if rates is None:
        raise StoreError("BUDGET_PRICE_UNKNOWN", 402)
    # Known official models have 1M context. Text byte bound includes a generous
    # protocol margin. Images and native search use the full model context bound.
    context = MODELS.model_context_limits.get(model)
    if context is None:
        raise StoreError("BUDGET_CONTEXT_UNKNOWN", 402)
    native = [t for t in body.get("tools", []) if t.get("type", "").startswith("web_search_")]
    images = any(
        b.get("type") == "image"
        for m in body["messages"]
        for b in m.get("content", [])
        if isinstance(b, dict)
    )
    text_bound = (
        2 * len(json.dumps(body, ensure_ascii=False).encode()) + 8192
        if not images and not native
        else context
    )
    if text_bound > context:
        raise StoreError("CONTEXT_TOO_LARGE", 400)
    rounds = 1 + sum(t.get("max_uses", 1) for t in native)
    return (
        Decimal(rounds)
        * (
            Decimal(text_bound) * Decimal(str(max(rates[0], rates[1])))
            + Decimal(body["max_tokens"]) * Decimal(str(rates[2]))
        )
        / 1_000_000
    ).quantize(Decimal("0.000000001"))


class BudgetStore:
    def ensure_budget(self, c, sid, uid, settings):
        row = c.execute("SELECT * FROM session_budgets WHERE session_id=?", (sid,)).fetchone()
        limit = Decimal(str(settings.session_budget_cny))
        if row:
            if row["user_id"] != uid:
                raise StoreError("NOT_FOUND", 404)
            if limit < Decimal(row["limit_cny"]):
                c.execute("UPDATE session_budgets SET limit_cny=? WHERE session_id=?", (str(limit), sid))
            return
        spent, uncertain = Decimal(0), Decimal(0)
        # Legacy USD costs remain USD in the ledger. For the new budget only,
        # known historical token counts are re-estimated using official CNY rates.
        for call in c.execute("SELECT * FROM calls WHERE session_id=? AND user_id=?", (sid, uid)):
            if call["currency"] == "CNY" and call["cost"] is not None:
                spent += Decimal(call["cost"])
            elif call["currency"] == "USD" and call["status"] == "success":
                amount = cny_cost(settings, call["model"], dict(call))
                if amount is None:
                    uncertain = limit
                else:
                    spent += amount
            else:
                uncertain = limit
        c.execute(
            "INSERT INTO session_budgets VALUES(?,?,?,?,?)",
            (sid, uid, str(limit), str(spent), str(uncertain)),
        )

    def reserve_budget(self, c, sid, uid, amount, settings):
        self.ensure_budget(c, sid, uid, settings)
        row = c.execute("SELECT * FROM session_budgets WHERE session_id=?", (sid,)).fetchone()
        pending = sum(
            (
                Decimal(x[0])
                for x in c.execute(
                    "SELECT amount_cny FROM budget_reservations WHERE session_id=? AND status='reserved'",
                    (sid,),
                )
            ),
            Decimal(0),
        )
        available = (
            Decimal(row["limit_cny"]) - Decimal(row["spent_cny"]) - Decimal(row["uncertain_cny"]) - pending
        )
        if amount > available:
            raise StoreError("BUDGET_EXCEEDED", 402)

    def settle_budget(self, c, call_id, cost):
        row = c.execute(
            "SELECT * FROM budget_reservations WHERE call_id=? AND status='reserved'", (call_id,)
        ).fetchone()
        if not row:
            return
        column = "spent_cny" if cost is not None else "uncertain_cny"
        charge = Decimal(str(cost)) if cost is not None else Decimal(row["amount_cny"])
        account = c.execute(
            f"SELECT {column} FROM session_budgets WHERE session_id=?", (row["session_id"],)
        ).fetchone()
        c.execute(
            f"UPDATE session_budgets SET {column}=? WHERE session_id=?",
            (str(Decimal(account[0]) + charge), row["session_id"]),
        )
        c.execute(
            "UPDATE budget_reservations SET status=?,charged_cny=? WHERE call_id=?",
            ("settled" if cost is not None else "uncertain", str(charge), call_id),
        )

    def budget_snapshot(self, sid, uid, settings):
        with self.connection(True) as c:
            c.execute("BEGIN IMMEDIATE")
            self.ensure_budget(c, sid, uid, settings)
            row = c.execute("SELECT * FROM session_budgets WHERE session_id=?", (sid,)).fetchone()
            pending = sum(
                (
                    Decimal(x[0])
                    for x in c.execute(
                        "SELECT amount_cny FROM budget_reservations WHERE session_id=? AND status='reserved'",
                        (sid,),
                    )
                ),
                Decimal(0),
            )
            available = (
                Decimal(row["limit_cny"])
                - Decimal(row["spent_cny"])
                - Decimal(row["uncertain_cny"])
                - pending
            )
            return {
                "session_id": sid,
                "currency": "CNY",
                "limit": row["limit_cny"],
                "spent": row["spent_cny"],
                "uncertain": row["uncertain_cny"],
                "reserved": str(pending),
                "available": str(max(available, Decimal(0))),
                "exhausted": available <= 0,
            }
