"""Text-only Off Formatter with voucher-excluded, audited consumption."""

import asyncio
import json
from decimal import Decimal

import httpx

from domain.errors import HarnessError
from llm.processor_stream import collect_processor_stream
from llm.providers import provider_for


class SiliconFlowClient:
    def __init__(self, settings, db, http):
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
        if ctx.stage != "formatter" or tools or on_text or on_thinking or effort != "off":
            raise HarnessError("PROCESSOR_INVALID_REQUEST")
        key = self.settings.siliconflow_key.get_secret_value()
        if not key:
            raise HarnessError("PROCESSOR_NOT_CONFIGURED")
        _, provider = provider_for(self.settings, "process")
        output_limit = provider.output_limits.get(model)
        context_limit = provider.context_limits.get(model)
        if output_limit is None or context_limit is None:
            raise HarnessError("PROCESSOR_LIMITS_UNKNOWN")
        converted = [{"role": "system", "content": system}]
        for message in messages:
            content = message["content"]
            if isinstance(content, list):
                if any(b.get("type") != "text" for b in content):
                    raise HarnessError("PROCESSOR_INVALID_REQUEST")
                content = "\n".join(b["text"] for b in content)
            converted.append({"role": message["role"], "content": content})
        # Conservative UTF-8 upper bound, independent of provider tokenizers.
        input_bound = 2 * len(json.dumps(converted, ensure_ascii=False).encode()) + 2048
        available_output = context_limit - input_bound
        if available_output < 256:
            raise HarnessError("PROCESSOR_CONTEXT_TOO_LARGE")
        body = {
            "model": model,
            "messages": converted,
            "max_tokens": min(max_tokens or self.settings.max_tokens, output_limit, available_output),
            "enable_thinking": False,
            "response_format": {"type": "json_object"},
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # No CNY reservation: excluded consumption is zero even at an exhausted
        # teaching budget. This is not a claim about the supplier's invoice.
        call_id = self.db.begin_call(
            ctx.user_id,
            ctx.session_id,
            ctx.stage,
            model,
            body,
            currency="CNY",
            basis="excluded:siliconflow:user-vouchers:not-supplier-invoice",
        )
        payload, usage = None, {}
        try:
            async with self.http.stream(
                "POST",
                self.settings.siliconflow_base.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + key},
                json=body,
            ) as response:
                if response.status_code != 200:
                    raise HarnessError("PROCESSOR_HTTP_ERROR", f"SiliconFlow HTTP {response.status_code}")
                try:
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        payload = await collect_processor_stream(response.aiter_lines())
                    else:
                        # Compatible gateways can return complete JSON even when
                        # streaming was requested. It undergoes identical checks.
                        await response.aread()
                        payload = response.json()
                except (ValueError, KeyError, TypeError) as exc:
                    raise HarnessError("PROCESSOR_INVALID_RESPONSE") from exc
            try:
                choices = payload["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("Expected one choice")
                choice = choices[0]
                text = choice["message"]["content"]
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or choice["finish_reason"] not in ("stop", "length")
                ):
                    raise ValueError("Invalid completion")
                if choice["message"].get("reasoning_content"):
                    raise ValueError("Unexpected thinking in Off")
                raw = payload["usage"]
                prompt, output = raw["prompt_tokens"], raw["completion_tokens"]
                details = raw.get("prompt_tokens_details") or {}
                hit = details.get("cached_tokens", 0)
                if any(type(n) is not int or n < 0 for n in (prompt, output, hit)) or hit > prompt:
                    raise ValueError("Invalid usage")
                usage = {
                    "input_tokens": prompt - hit,
                    "cache_read_input_tokens": hit,
                    "output_tokens": output,
                }
            except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
                raise HarnessError("PROCESSOR_INVALID_RESPONSE") from exc
            self.db.finish_call(call_id, response=payload, usage=usage, cost=Decimal(0))
            return {
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn" if choice["finish_reason"] == "stop" else "max_tokens",
                "usage": usage,
            }
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            code = (
                "CANCELLED"
                if cancelled
                else exc.code
                if isinstance(exc, HarnessError)
                else "PROCESSOR_TIMEOUT"
                if isinstance(exc, httpx.TimeoutException)
                else "PROCESSOR_TRANSPORT_ERROR"
            )
            if isinstance(exc, httpx.TransportError):
                # Safe diagnostics: no URLs, headers, credentials or exception
                # messages. Distinguish connect/read/pool timeouts in audit logs.
                payload = {"transport_error": {"type": type(exc).__name__}}
            self.db.finish_call(
                call_id,
                response=payload,
                usage=usage,
                cost=Decimal(0),
                error=code,
                status="cancelled" if cancelled else "failed",
            )
            if isinstance(exc, HarnessError) or cancelled:
                raise
            if isinstance(exc, Exception):
                raise HarnessError(code) from exc
            raise
