import asyncio
import base64
import io
from decimal import Decimal
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from PIL import Image
from pydantic import Field

from core.configuration import STAGES, TOOLS
from core.prompts import ASSISTANT
from core.settings import Settings
from domain.errors import HarnessError
from domain.schemas import QuestionBlock, StrictModel
from domain.syllabus import SyllabusQuery
from domain.validation import validate_html, validate_svg
from llm.contracts import LLM, CallContext
from llm.deepseek import parse_search, response_text
from storage.artifacts import ArtifactStore
from storage.client_store import StoreError
from storage.database import Database, dumps
from tools.pythonbox import run_nsjail, validate_python
from tools.syllabus import SyllabusService


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    max_results: int = Field(default=5, ge=1, le=10)


class AssistantArgs(StrictModel):
    task: str = Field(min_length=1, max_length=5000)


SyllabusArgs = SyllabusQuery


class ImageArgs(StrictModel):
    prompt: str = Field(min_length=1, max_length=5000)


class PythonArgs(StrictModel):
    code: str = Field(min_length=1, max_length=20000)
    input_artifact_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]] = Field(default_factory=list, max_length=4)


class RenderArgs(StrictModel):
    source: str = Field(min_length=1, max_length=100000)
    kind: str = Field(pattern="^(html|svg|css3d)$")
    width: int = Field(default=1024, ge=100, le=2000)
    height: int = Field(default=768, ge=100, le=2000)


class AskArgs(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=10)


class MaxThinkingArgs(StrictModel):
    reason: str = Field(min_length=1, max_length=300)


MODELS = {
    "Search": SearchArgs,
    "AssistantOnWeb": AssistantArgs,
    "Syllabus": SyllabusArgs,
    "GPTImage": ImageArgs,
    "PythonBox": PythonArgs,
    "Render": RenderArgs,
    "AskStudent": AskArgs,
    "RequestMaxThinking": MaxThinkingArgs,
}
DESCRIPTIONS = TOOLS.descriptions
BASE = ["Search", "AssistantOnWeb", "Syllabus", "AskStudent"]


def tool_definitions(names):
    return [
        {"name": n, "description": DESCRIPTIONS[n], "input_schema": MODELS[n].model_json_schema()}
        for n in sorted(set(names), key=lambda n: (BASE.index(n) if n in BASE else len(BASE), n))
    ]


class SkillExecutor:
    """One registry controls schemas, authorization and dispatch; services do the work."""

    def __init__(
        self, settings: Settings, db: Database, llm: LLM, http: httpx.AsyncClient, artifacts: ArtifactStore,
        syllabus: SyllabusService | None = None,
    ):
        self.settings, self.db, self.llm, self.http, self.artifacts = settings, db, llm, http, artifacts
        self.syllabus = syllabus or SyllabusService(settings)
        self.render_gate = asyncio.Semaphore(2)
        self.sandbox_gate = asyncio.Semaphore(settings.sandbox_concurrency)

    async def execute(self, ctx, name, args, allowed, paid_images=False):
        if name not in allowed or name not in MODELS:
            raise HarnessError("TOOL_NOT_ALLOWED")
        try:
            parsed = MODELS[name].model_validate(args)
        except ValueError as exc:
            raise HarnessError("TOOL_INVALID_ARGUMENTS") from exc
        if name == "Search":
            return await self.search(ctx, parsed)
        if name == "Syllabus":
            return await self.syllabus.search(parsed)
        if name == "AssistantOnWeb":
            return await self.assistant(ctx, parsed.task)
        if name == "AskStudent":
            return {"question": QuestionBlock(type="question", **parsed.model_dump()).model_dump()}
        if name == "GPTImage":
            return await self.image(ctx, parsed.prompt, paid_images)
        if name == "PythonBox":
            return await self.python(ctx, parsed)
        if name == "Render":
            return await self.render(ctx, parsed)
        raise HarnessError("TOOL_NOT_ALLOWED")

    async def search(self, ctx, args):
        payload = await self.llm.complete(
            CallContext(ctx.user_id, ctx.session_id, "search"),
            system=STAGES["Search"].instructions,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"Perform a web search for the query: {args.query}"}
                    ],
                }
            ],
            model=self.settings.search_model,
            effort=STAGES["Search"].effort,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        )
        return parse_search(payload, args.max_results)

    async def assistant(self, ctx, task):
        messages = [{"role": "user", "content": [{"type": "text", "text": task}]}]
        allowed = ["Search", "Syllabus"]  # No recursive AssistantOnWeb.
        calls = 0
        for _ in range(self.settings.max_assistant_steps):
            payload = await self.llm.complete(
                CallContext(ctx.user_id, ctx.session_id, "assistant"),
                system=ASSISTANT,
                messages=messages,
                model=getattr(self.settings, STAGES["AssistantOnWeb"].model_role + "_model"),
                effort=STAGES["AssistantOnWeb"].effort,
                tools=tool_definitions(allowed),
            )
            if payload.get("stop_reason") == "max_tokens":
                raise HarnessError("ASSISTANT_OUTPUT_TRUNCATED")
            messages.append({"role": "assistant", "content": payload["content"]})
            tools = [b for b in payload["content"] if b.get("type") == "tool_use"]
            if not tools:
                return {"report": response_text(payload)}
            results = []
            for tool in tools:
                calls += 1
                if calls > self.settings.max_tool_calls:
                    raise HarnessError("ASSISTANT_TOOL_LIMIT")
                try:
                    result = await self.execute(ctx, tool["name"], tool["input"], allowed)
                except HarnessError as exc:
                    if exc.code.startswith("BUDGET_"):
                        raise
                    result = {"error": exc.code}
                results.append({"type": "tool_result", "tool_use_id": tool["id"], "content": dumps(result)})
            messages.append({"role": "user", "content": results})
        raise HarnessError("ASSISTANT_STEP_LIMIT")

    async def image(self, ctx, prompt, authorized):
        if self.settings.image_credit > 0 and not authorized:
            raise HarnessError("PAID_IMAGE_APPROVAL_REQUIRED")
        if not self.settings.image_base or not self.settings.image_key.get_secret_value():
            raise HarnessError("IMAGE_NOT_CONFIGURED")
        body = {"model": self.settings.image_model, "prompt": prompt, "n": 1, "size": "1024x1024"}
        try:
            call_id = self.db.begin_call(
                ctx.user_id,
                ctx.session_id,
                "image",
                self.settings.image_model,
                body,
                currency="CNY",
                basis=f"configured:{self.settings.image_credit} credits/call",
                reservation=Decimal(self.settings.image_credit) / 100,
                settings=self.settings,
            )
        except StoreError as exc:
            raise HarnessError(exc.code) from exc
        payload, cost = None, None
        try:
            base = self.settings.image_base.rstrip("/")
            if not urlsplit(base).path:
                base += "/v1"
            endpoint = base if base.endswith("/images/generations") else base + "/images/generations"
            response = await self.http.post(
                endpoint,
                json=body,
                headers={"Authorization": "Bearer " + self.settings.image_key.get_secret_value()},
            )
            if response.status_code != 200:
                raise HarnessError("IMAGE_HTTP_ERROR", f"Image provider HTTP {response.status_code}")
            # A successful image request is charged even if decoding/storage later fails.
            cost = Decimal(self.settings.image_credit) / Decimal(100)
            try:
                payload = response.json()
            except ValueError as exc:
                raise HarnessError("IMAGE_INVALID_JSON") from exc
            data = payload.get("data", [{}])[0].get("b64_json")
            if not data:
                raise HarnessError(
                    "IMAGE_BASE64_REQUIRED",
                    "Provider must return b64_json; configure its image output format",
                )
            raw = base64.b64decode(data, validate=True)
            if len(raw) > self.settings.artifact_bytes:
                raise HarnessError("ARTIFACT_TOO_LARGE")
            with Image.open(io.BytesIO(raw)) as picture:
                picture.verify()
            artifact = self.artifacts.write(ctx, "image.png", "image/png", raw)
            # Avoid duplicating potentially multi-MB image base64 in the ledger.
            audit = {
                "created": payload.get("created"),
                "usage": payload.get("usage", {}),
                "artifact": artifact,
            }
            self.db.finish_call(call_id, response=audit, usage=payload.get("usage", {}), cost=cost)
            return artifact
        except BaseException as exc:
            code = (
                exc.code
                if isinstance(exc, HarnessError)
                else "CANCELLED"
                if isinstance(exc, asyncio.CancelledError)
                else "IMAGE_FAILED"
            )
            self.db.finish_call(
                call_id,
                error=code + (":" + type(exc).__name__ if code == "IMAGE_FAILED" else ""),
                cost=cost,
                status="cancelled" if code == "CANCELLED" else "failed",
            )
            if isinstance(exc, HarnessError) or not isinstance(exc, Exception):
                raise
            raise HarnessError(code) from exc

    async def python(self, ctx, args):
        validate_python(args.code, args.input_artifact_ids)
        if not self.settings.sandbox_enabled:
            raise HarnessError("SANDBOX_UNAVAILABLE")
        inputs = {
            item: self.artifacts.read(ctx, item, render_only=True)
            for item in args.input_artifact_ids
        }
        try:
            async with self.sandbox_gate:
                result = await run_nsjail(self.settings, args.code, inputs)
            log = (result.get("stdout", "") + "\n" + result.get("stderr", ""))[:131072]
            log_artifact = self.artifacts.write(ctx, "python.log", "text/plain", log.encode())
            files = [log_artifact]
            for file in result.get("files", [])[:4]:
                if (file.get("name"), file.get("mime")) not in (
                    ("output.png", "image/png"),
                    ("output.svg", "image/svg+xml"),
                ):
                    raise HarnessError("SANDBOX_INVALID_RESPONSE")
                raw = base64.b64decode(file["data"], validate=True)
                if file["mime"] == "image/png":
                    with Image.open(io.BytesIO(raw)) as picture:
                        picture.verify()
                else:
                    validate_svg(raw.decode("utf-8"))
                files.append(self.artifacts.write(ctx, "python-" + file["name"], file["mime"], raw))
            return {"exit_code": result["exit_code"], "log": log, "files": files}
        except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
            raise HarnessError("SANDBOX_FAILED") from exc

    async def render(self, ctx, args):
        if not self.settings.render_enabled:
            raise HarnessError("RENDER_UNAVAILABLE")
        if args.kind == "svg":
            validate_svg(args.source)
        else:
            validate_html(args.source)
        from playwright.async_api import Error, async_playwright

        browser_path = self.settings.browser_path
        if not browser_path:
            candidates = list(
                (self.settings.data_dir / "browsers").glob(
                    "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe"
                )
            )
            if candidates:
                browser_path = str(candidates[0])
        async with self.render_gate, async_playwright() as p:
            browser = None
            try:
                browser = await p.chromium.launch(
                    headless=True,
                    executable_path=browser_path,
                    args=["--disable-dev-shm-usage"],
                )
                context = await browser.new_context(
                    viewport={"width": args.width, "height": args.height},
                    java_script_enabled=False,
                    offline=True,
                    service_workers="block",
                    accept_downloads=False,
                )
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                source = args.source
                if args.kind == "svg":
                    # SVG is the complete viewport, without browser body margins
                    # clipping its right/bottom edge. viewBox keeps its geometry.
                    source = (
                        "<style>html,body{margin:0;width:100%;height:100%;overflow:hidden}"
                        "body>svg{display:block;width:100%;height:100%}</style>" + source
                    )
                await page.set_content(
                    "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
                    + source,
                    wait_until="load",
                    timeout=10000,
                )
                raw = await page.screenshot(type="png", timeout=10000, animations="disabled")
                return self.artifacts.write(ctx, "render-output.png", "image/png", raw)
            except Error as exc:
                raise HarnessError(
                    "RENDER_FAILED", "Install Playwright Chromium or configure BROWSER_PATH"
                ) from exc
            finally:
                if browser:
                    await browser.close()
