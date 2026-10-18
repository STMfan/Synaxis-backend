"""Bounded owner-visible round traces, without signatures or request prompts."""

from domain.schemas import FileBlock


class RoundLog:
    def __init__(self, ctx, round_number, effort, artifacts, db):
        self.ctx, self.round, self.effort = ctx, round_number, effort
        self.artifacts, self.db = artifacts, db
        self.remaining = max(0, artifacts.settings.artifact_bytes - 512)
        self.parts = {"thinking": [], "output": []}
        self.truncated = False

    def capture(self, field, text):
        encoded = text.encode("utf-8")
        if len(encoded) > self.remaining:
            text = encoded[: self.remaining].decode("utf-8", errors="ignore")
            encoded = text.encode("utf-8")
            self.truncated = True
        if text:
            self.parts[field].append(text)
            self.remaining -= len(encoded)

    def sink(self, field, stream):
        async def receive(chunk):
            self.capture(field, chunk)
            await stream.text(chunk)

        return receive

    def finish(self, status):
        thinking = "".join(self.parts["thinking"])
        output = "".join(self.parts["output"])
        text = (
            f"Synaxis round={self.round} effort={self.effort} status={status}\n"
            "[provider_thinking]\n"
            + (thinking or "(No thinking returned by provider.)")
            + "\n[output]\n"
            + output
            + ("\n[truncated: artifact byte limit reached]" if self.truncated else "")
            + "\n"
        )
        artifact = self.artifacts.write(
            self.ctx, f"round-{self.round}.log", "text/plain", text.encode("utf-8")
        )
        self.db.event(self.ctx.session_id, "artifact", {"round": self.round, "status": status, **artifact})
        return FileBlock(type="localfile", artifact_id=artifact["artifact_id"], label=artifact["name"])
