"""Deliver an already captured snapshot without retaining native DB access."""

from __future__ import annotations

import threading
from collections.abc import Callable

from starlette.responses import StreamingResponse

from grag.core.engine import Engine
from grag.transfer import capture_snapshot


class SnapshotResponse(StreamingResponse):
    _close: Callable[[], None]

    @classmethod
    def capture(cls, engine: Engine, *, headers: dict[str, str]) -> SnapshotResponse:
        snapshot = capture_snapshot(engine)
        stream = snapshot.__enter__()
        lock = threading.Lock()
        closed = False

        def close() -> None:
            nonlocal closed
            with lock:
                if not closed:
                    closed = True
                    snapshot.__exit__(None, None, None)

        def body():
            try:
                while chunk := stream.read(64 * 1024):
                    yield chunk
            finally:
                close()

        try:
            response = cls(body(), headers=headers, media_type="application/x-ndjson")
            response._close = close
            return response
        except BaseException:
            close()
            raise

    def close(self) -> None:
        """Release the spool, including when iteration never starts."""
        self._close()

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.close()
