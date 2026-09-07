"""Bound HTTP bodies before JSON parsing, including chunked MCP requests."""
from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from grag.core.errors import ResourceLimitError
from grag.core.limits import MAX_REQUEST_BYTES


class RequestLimitMiddleware:
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > MAX_REQUEST_BYTES:
                await JSONResponse(ResourceLimitError("request_bytes", MAX_REQUEST_BYTES).to_dict(), status_code=413)(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        payload = b"".join(chunks)
        delivered = False

        async def limited_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return await receive()

        await self.app(scope, limited_receive, send)
