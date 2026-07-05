"""Coverage for the request-body size cap (BodySizeLimitMiddleware)."""
import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from backend.api.body_limit import BodySizeLimitMiddleware, BodyTooLargeError

LIMIT = 1024


@pytest.fixture()
def client():
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"n": len(body)}

    return TestClient(app)


def test_under_limit_passes(client):
    r = client.post("/echo", content=b"x" * LIMIT)
    assert r.status_code == 200
    assert r.json() == {"n": LIMIT}


def test_content_length_over_limit_rejected(client):
    r = client.post("/echo", content=b"x" * (LIMIT + 1))
    assert r.status_code == 413


def test_empty_body_passes(client):
    r = client.post("/echo", content=b"")
    assert r.status_code == 200


def test_get_without_body_unaffected():
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    r = TestClient(app).get("/ping")
    assert r.status_code == 200


def test_zero_disables_cap():
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=0)

    @app.post("/echo")
    async def echo(request: Request):
        return {"n": len(await request.body())}

    r = TestClient(app).post("/echo", content=b"x" * (LIMIT * 4))
    assert r.status_code == 200


def test_streamed_body_without_content_length_rejected():
    """Drive the ASGI interface directly: chunked body, no content-length."""
    async def inner_app(scope, receive, send):
        while True:
            msg = await receive()
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BodySizeLimitMiddleware(inner_app, max_bytes=LIMIT)
    scope = {"type": "http", "method": "POST", "path": "/echo", "headers": []}
    chunks = [b"x" * 512, b"x" * 512, b"x" * 512]
    sent = []

    async def receive():
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    start = [m for m in sent if m["type"] == "http.response.start"]
    assert start and start[0]["status"] == 413


def test_body_too_large_error_is_exception():
    assert issubclass(BodyTooLargeError, Exception)
