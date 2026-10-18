from collections import deque

from fastapi.responses import JSONResponse


class BodyLimit:
    def __init__(self, app, limit=32 * 1024**2):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        size, messages = 0, deque()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                return await JSONResponse(
                    {"code": "BODY_TOO_LARGE", "detail": "Request body exceeds limit"}, 413
                )(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.popleft() if messages else await receive()

        await self.app(scope, replay, send)
