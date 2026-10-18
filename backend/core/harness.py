import asyncio

from core.configuration import STAGES
from core.output_stream import OutputStream
from core.presentation import parse_expert, recover_presentation, unwrap_document, validate_presentation
from core.prompts import EXPERTS, FORMATTER, PLANNER, expert_prompt
from core.round_log import RoundLog
from core.settings import Settings
from domain.errors import HarnessError
from domain.schemas import Plan, Presentation
from llm.contracts import LLM, CallContext
from llm.deepseek import response_text
from llm.providers import supports_max
from storage.artifacts import ArtifactStore
from storage.database import Database, dumps
from tools.executor import BASE, MaxThinkingArgs, SkillExecutor, tool_definitions


class Harness:
    def __init__(
        self, settings: Settings, db: Database, llm: LLM, executor: SkillExecutor, artifacts: ArtifactStore
    ):
        self.settings, self.db, self.llm, self.executor, self.artifacts = (
            settings,
            db,
            llm,
            executor,
            artifacts,
        )
        self.tasks: dict[str, asyncio.Task] = {}
        self.accepting = True
        self.gate = asyncio.Semaphore(settings.max_concurrent_runs)
        self.max_pending = settings.max_concurrent_runs * 8

    def ensure_capacity(self):
        if not self.accepting:
            raise HarnessError("SERVER_BUSY")
        if len(self.tasks) >= self.max_pending:
            raise HarnessError("SERVER_BUSY")

    def start(self, session_id):
        if session_id in self.tasks:
            raise HarnessError("SESSION_BUSY")
        task = asyncio.create_task(self.run(session_id), name=f"tutor-{session_id}")
        self.tasks[session_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(session_id, None))

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, session_id):
        session = self.db.session(session_id)
        if not session or session["status"] != "queued":
            return
        state = session["state"]
        ctx = CallContext(session["user_id"], session_id, "expert")
        try:
            async with self.gate, asyncio.timeout(self.settings.session_timeout):
                self.db.save_session(session_id, "running", state)
                self.db.event(session_id, "running", {})
                if state["plan"] is None:
                    current_input = state.pop("pending_input", state["request"])
                    plan = await self.plan(ctx, current_input)
                    state["plan"] = plan.model_dump()
                    expert = EXPERTS[plan.expert]
                    if current_input.get("images") and not expert.images:
                        raise HarnessError("EXPERT_IMAGES_UNSUPPORTED", "诗云仅接受文本，请先转录语文图片。")
                    state["messages"] = [{"role": "user", "content": self.user_content(current_input, plan)}]
                    self.db.save_session(session_id, "running", state)
                    self.db.event(session_id, "plan", state["plan"])
                    if plan.thinking == "max":
                        state["effort"], state["max_thinking_used"] = "max", True
                        self.db.event(
                            session_id,
                            "thinking",
                            {"effort": "max", "source": "planner", "reason": plan.max_reason},
                        )
                plan = Plan.model_validate(state["plan"])
                expert = EXPERTS[plan.expert]
                ctx = CallContext(ctx.user_id, ctx.session_id, "expert", expert.model_role)
                model = getattr(self.settings, expert.model_role + "_model")
                while state["steps"] < self.settings.max_steps:
                    state["steps"] += 1
                    allow_max = (
                        expert.allow_max_thinking
                        and supports_max(self.settings, expert.model_role)
                        and state["request"].get("allow_max_thinking", True)
                        and not state.get("max_thinking_used", False)
                    )
                    allowed = [*BASE, *plan.skills, *(["RequestMaxThinking"] if allow_max else [])]
                    effort = state.get("effort", expert.effort)
                    output = OutputStream(self.db, session_id, state["steps"], effort)
                    reasoning = OutputStream(
                        self.db, session_id, state["steps"], effort, channel="reasoning", lazy=True
                    )
                    trace = RoundLog(ctx, state["steps"], effort, self.artifacts, self.db)

                    try:
                        payload = await self.llm.complete(
                            ctx,
                            system=expert_prompt(plan.expert, plan.skills, allow_max=allow_max),
                            messages=state["messages"],
                            model=model,
                            effort=effort,
                            tools=tool_definitions(allowed),
                            on_text=trace.sink("output", output),
                            on_thinking=trace.sink("thinking", reasoning),
                            max_tokens=self.settings.expert_max_tokens,
                        )
                    except BaseException:
                        output.end("aborted")
                        reasoning.end("aborted")
                        trace.finish("aborted")
                        raise
                    output.end("received")
                    reasoning.end("received")
                    log = trace.finish("received")
                    if payload.get("stop_reason") in ("max_tokens", "refusal"):
                        raise HarnessError("EXPERT_OUTPUT_INCOMPLETE")
                    # Preserve thinking blocks verbatim in tool-call history as required by the provider.
                    state["messages"].append({"role": "assistant", "content": payload["content"]})
                    text = response_text(payload)
                    tools = [b for b in payload["content"] if b.get("type") == "tool_use"]
                    if any(
                        not isinstance(b.get("id"), str)
                        or not b["id"]
                        or not isinstance(b.get("name"), str)
                        or not b["name"]
                        or not isinstance(b.get("input"), dict)
                        for b in tools
                    ):
                        raise HarnessError("PROVIDER_INVALID_TOOL_CALL")
                    if len({b["id"] for b in tools}) != len(tools):
                        raise HarnessError("PROVIDER_INVALID_TOOL_CALL")
                    if sum(t.get("name") == "AskStudent" for t in tools) > 1:
                        raise HarnessError("MULTIPLE_QUESTIONS")
                    # Format each public expert output before continuing its tool round.
                    if text and not any(t.get("name") == "AskStudent" for t in tools):
                        await self.present(ctx, state, text, log=log)
                    results, question = [], None
                    for tool in tools:
                        state["tool_calls"] += 1
                        if state["tool_calls"] > self.settings.max_tool_calls:
                            raise HarnessError("TOOL_CALL_LIMIT")
                        try:
                            if tool["name"] == "RequestMaxThinking":
                                if not allow_max or state.get("max_thinking_used"):
                                    raise HarnessError("MAX_THINKING_NOT_ALLOWED")
                                try:
                                    args = MaxThinkingArgs.model_validate(tool["input"])
                                except ValueError as exc:
                                    raise HarnessError("TOOL_INVALID_ARGUMENTS") from exc
                                state["effort"], state["max_thinking_used"] = "max", True
                                result = {"effort": "max", "applies_to": "next_round"}
                                self.db.event(
                                    session_id,
                                    "thinking",
                                    {"effort": "max", "source": "expert", "reason": args.reason},
                                )
                            else:
                                result = await self.executor.execute(
                                    ctx,
                                    tool["name"],
                                    tool["input"],
                                    allowed,
                                    paid_images=state["request"]["allow_paid_images"],
                                )
                        except HarnessError as exc:
                            if exc.code.startswith("BUDGET_"):
                                raise
                            result = {"error": exc.code, "detail": exc.detail}
                        self.db.event(session_id, "tool", {"name": tool["name"], "result": result})
                        results.append(
                            {"type": "tool_result", "tool_use_id": tool["id"], "content": dumps(result)}
                        )
                        if "question" in result:
                            question = result["question"]
                    if results:
                        state["messages"].append({"role": "user", "content": results})
                    if question:
                        state["question"] = question
                        await self.present(ctx, state, text or question["prompt"], question, log=log)
                        self.db.save_session(session_id, "waiting", state)
                        self.db.event(session_id, "waiting", {"question": question})
                        return
                    self.db.save_session(session_id, "running", state)
                    if not tools:
                        if not text.strip():
                            raise HarnessError("EXPERT_EMPTY_RESPONSE")
                        self.db.save_session(session_id, "completed", state)
                        self.db.event(session_id, "completed", {})
                        return
                raise HarnessError("STEP_LIMIT_REACHED")
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            code = (
                "CANCELLED"
                if cancelled
                else "SESSION_TIMEOUT"
                if isinstance(exc, TimeoutError)
                else exc.code
                if isinstance(exc, HarnessError)
                else "INTERNAL_ERROR"
            )
            state["error"] = code
            status = "cancelled" if cancelled else "failed"
            self.db.save_session(session_id, status, state)
            self.db.event(session_id, status, {"code": code})
            if cancelled:
                raise

    @staticmethod
    def user_content(request, plan):
        content = [
            {
                "type": "text",
                "text": dumps(
                    {
                        "plan": plan.model_dump(),
                        "student": {k: v for k, v in request.items() if k != "images"},
                    }
                ),
            }
        ]
        content.extend(
            {"type": "image", "source": {"type": "base64", **image}} for image in request["images"]
        )
        return content

    async def plan(self, ctx, request):
        payload = await self.llm.complete(
            CallContext(ctx.user_id, ctx.session_id, "planner"),
            system=PLANNER,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": dumps({k: v for k, v in request.items() if k != "images"})},
                        *[{"type": "image", "source": {"type": "base64", **i}} for i in request["images"]],
                    ],
                }
            ],
            model=getattr(self.settings, STAGES["Planner"].model_role + "_model"),
            effort=STAGES["Planner"].effort,
            max_tokens=min(
                self.settings.max_tokens, STAGES["Planner"].max_tokens or self.settings.max_tokens
            ),
        )
        try:
            if payload.get("stop_reason") == "max_tokens":
                raise ValueError("Truncated plan")
            plan = Plan.model_validate_json(response_text(payload))
            expert = EXPERTS[plan.expert]
            if plan.subject not in expert.subjects:
                raise ValueError("Expert subject mismatch")
            plan.skills = sorted(set(plan.skills) - set(BASE))
            if not set(plan.skills) <= set(expert.exskills):
                raise ValueError("Expert skill mismatch")
            if plan.thinking == "max" and (
                plan.expert not in STAGES["Planner"].max_experts
                or plan.complexity != "extreme"
                or not plan.max_reason.strip()
                or not expert.allow_max_thinking
                or not request.get("allow_max_thinking", True)
                or not supports_max(self.settings, expert.model_role)
            ):
                raise ValueError("Planner Max is restricted to extreme STEM problems with opt-in")
            return plan
        except ValueError as exc:
            raise HarnessError("PLANNER_INVALID_OUTPUT") from exc

    async def present(self, ctx, state, text, question=None, *, log=None):
        with self.db.connection() as c:
            rows = c.execute(
                "SELECT id FROM artifacts WHERE session_id=? AND user_id=?", (ctx.session_id, ctx.user_id)
            ).fetchall()
        artifact_ids = [r["id"] for r in rows]
        warning = None
        validation_error = None
        try:
            presentation = parse_expert(text, artifact_ids, question)
        except (ValueError, HarnessError) as exc:
            presentation = None
            validation_error = str(exc)[:500]
        if presentation is None:
            try:
                presentation = await self.format(ctx, text, question, artifact_ids, validation_error)
            except HarnessError as exc:
                if exc.code.startswith("BUDGET_"):
                    raise
                warning = exc.code
                presentation = recover_presentation(text, artifact_ids, question)
            except ValueError:
                warning = "FORMATTER_INVALID_OUTPUT"
                presentation = recover_presentation(text, artifact_ids, question)
        if log is None:
            trace = RoundLog(ctx, state["steps"], "off", self.artifacts, self.db)
            trace.capture("output", text)
            log = trace.finish("received")
        presentation.blocks.append(log)
        result = presentation.model_dump()
        state["presentations"].append(result)
        self.db.commit_presentation(ctx.session_id, state, result, warning)

    async def format(self, ctx, text, question, artifact_ids, validation_error=None):
        payload = await self.llm.complete(
            CallContext(ctx.user_id, ctx.session_id, "formatter"),
            system=FORMATTER,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": dumps(
                                {
                                    "answer": text,
                                    "question": question,
                                    "artifact_ids": artifact_ids,
                                    "validation_error": validation_error,
                                }
                            ),
                        }
                    ],
                }
            ],
            model=self.settings.process_model,
            effort=STAGES["Formatter"].effort,
            max_tokens=min(
                self.settings.max_tokens, STAGES["Formatter"].max_tokens or self.settings.max_tokens
            ),
        )
        if payload.get("stop_reason") == "max_tokens":
            raise ValueError("Truncated presentation")
        presentation = Presentation.model_validate_json(unwrap_document(response_text(payload), {"json"}))
        # Server-owned questions can be appended exactly if the model omits one.
        # Changed, duplicated or invented questions still fail strict validation.
        return validate_presentation(presentation, artifact_ids, question, append_question=True)

    def cancel(self, session_id):
        task = self.tasks.get(session_id)
        if task:
            session = self.db.session(session_id)
            if session and session["status"] == "queued":
                session["state"]["error"] = "CANCELLED"
                self.db.save_session(session_id, "cancelled", session["state"])
                self.db.event(session_id, "cancelled", {"code": "CANCELLED"})
            task.cancel()
            return
        session = self.db.session(session_id)
        if session and session["status"] in ("queued", "waiting"):
            self.db.save_session(session_id, "cancelled", session["state"])
            self.db.event(session_id, "cancelled", {"code": "CANCELLED"})
