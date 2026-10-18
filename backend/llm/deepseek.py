import asyncio
from decimal import Decimal

import httpx

from core.configuration import MODELS
from core.settings import Settings
from domain.errors import HarnessError
from llm.streaming import assemble, sse_events
from storage.budget import cny_cost, reservation_cost
from storage.client_store import StoreError
from storage.database import Database
from storage.pricing import price_snapshot, pricing_band


def token_cost(settings: Settings, model: str, usage: dict) -> Decimal | None:
    rates = settings.price_rates.get(model)
    if rates is None:
        return None
    # Anthropic input_tokens excludes cache read/write tokens.
    hit = int(usage.get("cache_read_input_tokens", 0))
    miss = int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0))
    output = int(usage.get("output_tokens", 0))
    divisor = Decimal(2) if pricing_band(settings) == "off_peak" else Decimal(1)
    return (
        (
            Decimal(hit) * Decimal(str(rates[0]))
            + Decimal(miss) * Decimal(str(rates[1]))
            + Decimal(output) * Decimal(str(rates[2]))
        )
        / Decimal(1_000_000)
        / divisor
    ).quantize(Decimal("0.000000001"))


class DeepSeekClient:
    def __init__(self, settings: Settings, db: Database, http: httpx.AsyncClient):
        self.settings, self.db, self.http = settings, db, http

    async def complete(
        self,
        ctx,
        *,
        system,
        messages,
        model,
        effort=None,
        tools=None,
        on_text=None,
        on_thinking=None,
        max_tokens=None,
    ):
        if effort is None:
            effort = (
                MODELS.literature.effort if model == self.settings.literature_model else MODELS.main.effort
            )
        if effort not in ("off", "low", "high", "max"):
            raise HarnessError("INVALID_THINKING_LEVEL")
        body = {
            "model": model,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "thinking": {"type": "disabled" if effort == "off" else "enabled"},
            "system": system,
            "messages": messages,
            "metadata": {"user_id": str(ctx.user_id)},
        }
        if effort != "off":
            body["output_config"] = {"effort": effort}
        if on_text or on_thinking:
            body["stream"] = True
        if tools:
            body["tools"] = tools
        key = self.settings.deepseek_key.get_secret_value()
        if not key:
            raise HarnessError("PROVIDER_NOT_CONFIGURED")
        snapshot = price_snapshot(self.settings, model)
        try:
            # Every stage can request the official ceiling; reserve only what
            # fits this session, including native-search service-side rounds.
            if ctx.session_id:
                if model not in self.settings.price_rates_cny:
                    raise StoreError("BUDGET_PRICE_UNKNOWN", 402)
                limit = MODELS.model_output_limits.get(model)
                if limit is None:
                    raise StoreError("BUDGET_CONTEXT_UNKNOWN", 402)
                body["max_tokens"] = min(body["max_tokens"], limit)
                available = Decimal(
                    self.db.budget_snapshot(ctx.session_id, ctx.user_id, self.settings)["available"]
                )
                low, high = 256, body["max_tokens"]
                minimum = {**body, "max_tokens": low}
                if reservation_cost(self.settings, model, minimum) > available:
                    raise StoreError("BUDGET_EXCEEDED", 402)
                # Reservation still happens atomically below. A concurrent change
                # can reject the call, but can never overspend the session budget.
                while low < high:
                    middle = (low + high + 1) // 2
                    if reservation_cost(self.settings, model, {**body, "max_tokens": middle}) <= available:
                        low = middle
                    else:
                        high = middle - 1
                body["max_tokens"] = low
            reserve = reservation_cost(self.settings, model, body)
            call_id = self.db.begin_call(
                ctx.user_id,
                ctx.session_id,
                ctx.stage,
                model,
                body,
                currency="CNY",
                basis=snapshot.basis,
                reservation=reserve,
                settings=self.settings,
            )
        except StoreError as exc:
            raise HarnessError(exc.code) from exc
        payload = None
        try:
            headers = {
                "x-api-key": key,
                "Authorization": f"Bearer {key}",
                "anthropic-version": "2023-06-01",
            }
            url = self.settings.deepseek_base.rstrip("/") + "/messages"
            if on_text or on_thinking:
                async with self.http.stream("POST", url, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        raise HarnessError("PROVIDER_HTTP_ERROR", f"DeepSeek HTTP {response.status_code}")
                    if not response.headers.get("content-type", "").startswith("text/event-stream"):
                        raise HarnessError("PROVIDER_INVALID_STREAM")
                    try:
                        payload = await assemble(sse_events(response.aiter_lines()), on_text, on_thinking)
                    except ValueError as exc:
                        raise HarnessError("PROVIDER_INVALID_STREAM") from exc
            else:
                response = await self.http.post(url, json=body, headers=headers)
                if response.status_code != 200:
                    raise HarnessError("PROVIDER_HTTP_ERROR", f"DeepSeek HTTP {response.status_code}")
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise HarnessError("PROVIDER_INVALID_RESPONSE") from exc
            try:
                if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
                    raise ValueError("Invalid content")
                if any(
                    not isinstance(block, dict) or not isinstance(block.get("type"), str)
                    for block in payload["content"]
                ):
                    raise ValueError("Invalid content block")
                usage = payload.get("usage", {})
                if not isinstance(usage, dict) or any(
                    type(usage.get(k, 0)) is not int or usage.get(k, 0) < 0
                    for k in (
                        "input_tokens",
                        "output_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                ):
                    raise ValueError("Invalid usage")
            except (ValueError, TypeError) as exc:
                raise HarnessError("PROVIDER_INVALID_RESPONSE") from exc
            self.db.finish_call(
                call_id,
                response=payload,
                usage=usage,
                cost=cny_cost(self.settings, model, usage, snapshot=snapshot)
                if "input_tokens" in usage and "output_tokens" in usage
                else None,
            )
            return payload
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            code = (
                "CANCELLED"
                if cancelled
                else exc.code
                if isinstance(exc, HarnessError)
                else "PROVIDER_TRANSPORT_ERROR"
            )
            self.db.finish_call(
                call_id, response=payload, error=code, status="cancelled" if cancelled else "failed"
            )
            if isinstance(exc, HarnessError) or cancelled:
                raise
            if isinstance(exc, Exception):
                raise HarnessError(code) from exc
            raise


def response_text(payload: dict) -> str:
    return "\n".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def parse_search(payload: dict, limit=5) -> dict:
    blocks = payload.get("content", [])
    results = [b for b in blocks if b.get("type") == "web_search_tool_result"]
    if not results:
        raise HarnessError("SEARCH_NO_RESULT_BLOCK")
    snippets = {}
    for b in blocks:
        if b.get("type") == "text":
            for cite in b.get("citations", []):
                if cite.get("url") and cite.get("cited_text"):
                    snippets.setdefault(cite["url"], cite["cited_text"])
    sources, seen = [], set()
    for block in results:
        content = block.get("content", [])
        if isinstance(content, dict):
            raise HarnessError("SEARCH_SERVER_ERROR")
        for item in content:
            url = item.get("url", "")
            if (
                item.get("type") != "web_search_result"
                or not url.startswith(("https://", "http://"))
                or url in seen
            ):
                continue
            seen.add(url)
            sources.append(
                {
                    "url": url,
                    "title": item.get("title", ""),
                    "published_at": item.get("page_age"),
                    "snippet": snippets.get(url),
                }
            )
    return {"sources": sources[:limit], "truncated": len(sources) > limit}
