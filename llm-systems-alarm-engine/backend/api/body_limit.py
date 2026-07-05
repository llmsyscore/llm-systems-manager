"""ASGI middleware capping HTTP request-body size (413 past the limit)."""

import json
import logging

logger = logging.getLogger(__name__)


class BodyTooLargeError(Exception):
    """Raised by the wrapped receive() when the streamed body passes the cap."""


class BodySizeLimitMiddleware:
    """Rejects HTTP requests whose body exceeds max_bytes: declared
    Content-Length is checked up front, streamed bodies are counted."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await self._send_413(send)
                    return
                break

        received = 0
        response_started = False

        async def wrapped_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise BodyTooLargeError()
            return message

        async def wrapped_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except BodyTooLargeError:
            logger.warning("request body exceeded %d bytes on %s — rejected",
                           self.max_bytes, scope.get("path", "?"))
            if response_started:
                raise
            await self._send_413(send)

    async def _send_413(self, send):
        body = json.dumps({"detail": "request body too large"}).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
