"""Coalesce public text deltas while retaining replayable SSE event IDs."""

import time
import uuid


class OutputStream:
    def __init__(self, db, session_id, round_number, effort, *, channel="output", lazy=False):
        self.db, self.session_id = db, session_id
        self.id, self.sequence, self.buffer, self.last_flush = uuid.uuid4().hex, 0, "", 0
        self.channel, self.started = channel, False
        self.start_data = {"stream_id": self.id, "round": round_number, "effort": effort}
        if not lazy:
            self.start()

    def start(self):
        if not self.started:
            self.db.event(self.session_id, self.channel + "_start", self.start_data)
            self.started = True

    async def text(self, text):
        if not text:
            return
        self.start()
        self.buffer += text
        if len(self.buffer) >= 512 or time.monotonic() - self.last_flush >= 0.08:
            self.flush()

    def flush(self):
        while self.buffer:
            part, self.buffer = self.buffer[:4096], self.buffer[4096:]
            self.sequence += 1
            self.db.event(
                self.session_id,
                self.channel + "_delta",
                {"stream_id": self.id, "sequence": self.sequence, "text": part},
            )
        self.last_flush = time.monotonic()

    def end(self, status):
        if not self.started:
            return
        self.flush()
        self.db.event(self.session_id, self.channel + "_end", {"stream_id": self.id, "status": status})
