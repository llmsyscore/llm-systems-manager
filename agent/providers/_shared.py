"""Helpers shared by the llama/vllm provider modules — OpenAI passthrough,
svcconfig apply sequence, log tail/stream scaffolding (#360)."""

from __future__ import annotations

import json
import logging
import os
import queue as _queue_lib
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import requests
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

import stream_pool  # type: ignore[import-not-found]  # sibling at agent root

log = logging.getLogger("llm-systems-agent.providers._shared")

OPENAI_READ_TIMEOUT_S = 600.0

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


BENCH_SERIES = ("ppt", "gen", "pg")


def bench_row_series(row: dict) -> "Optional[str]":
    """Which chart series a llama-bench result row belongs to."""
    s = row.get("series")
    if s in BENCH_SERIES:
        return s
    n_p = row.get("n_prompt") or 0
    n_g = row.get("n_gen") or 0
    if n_p > 0 and n_g > 0:
        return "pg"
    if n_p > 0:
        return "ppt"
    if n_g > 0:
        return "gen"
    return None


def bench_maxes(rows: list) -> dict:
    """Best avg_ts per series across a model's result rows."""
    out: "dict[str, Optional[float]]" = {k: None for k in BENCH_SERIES}
    for row in rows or []:
        series = bench_row_series(row)
        if not series:
            continue
        val = float(row.get("avg_ts") or 0)
        out[series] = val if out[series] is None else max(out[series], val)
    return out


def post_tool_run(ctx, tool: str, provider: str, run_id: str, model_id: str,
                  ok: bool, summary: dict) -> None:
    """Record a finished run in the manager's tool ledger; best effort so a
    closed dashboard no longer loses the row (#772)."""
    base = (getattr(ctx.config, "MANAGER_URL", "") or "").rstrip("/")
    state = getattr(ctx, "state", None) or {}
    token = state.get("token") or ""
    if not base or not token or not model_id:
        return
    body = {"tool": tool, "provider": provider, "model_id": model_id,
            "ok": bool(ok), "run_id": run_id or "",
            "agent_id": state.get("agent_id") or ""}
    body.update({k: v for k, v in (summary or {}).items() if v is not None})
    try:
        ctx.post_session.post(f"{base}/api/tools/runs", json=body, timeout=10,
                              headers={"Authorization": f"Bearer {token}"})
    except Exception as e:
        log.debug("tool-run ledger post failed: %s", e)


def pool_guarded_sse(gen) -> StreamingResponse:
    """Stream-pool-guarded SSE response around a byte generator."""
    if not stream_pool.POOL.try_acquire():
        raise HTTPException(status_code=503,
                            detail="agent at stream capacity; retry shortly")
    return StreamingResponse(
        stream_pool.guarded_async(gen),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def bench_replay_iter(replay, cond, is_active, last_event_id,
                      *, wait_timeout=10.0) -> Iterator[bytes]:
    """Replay-buffer SSE generator: resume from Last-Event-ID, wait on cond for
    records, keepalive while active, end idle streams with a synthetic done."""
    with cond:
        cur_run = replay.run_id
        last_seq = replay.seq_for(last_event_id)
    while True:
        with cond:
            if cur_run and replay.run_id != cur_run:
                return
            cur_run = replay.run_id
            new = replay.records_after_seq(last_seq)
            if not new:
                cond.wait(timeout=wait_timeout)
                if cur_run and replay.run_id != cur_run:
                    return
                new = replay.records_after_seq(last_seq)
        if not new:
            if not is_active():
                yield (b'data: {"type":"done","ok":false,'
                       b'"error":"no active job"}\n\n')
                return
            yield b'data: {"type":"keepalive"}\n\n'
            continue
        for rec in new:
            yield f"id: {rec['id']}\ndata: {json.dumps(rec['event'])}\n\n".encode()
            last_seq = rec["seq"]
            if rec["event"].get("type") == "done":
                return


def bench_replay_sse(replay, cond, is_active, last_event_id) -> StreamingResponse:
    """Stream-pool-guarded SSE response around the bench replay generator."""
    return pool_guarded_sse(bench_replay_iter(replay, cond, is_active, last_event_id))


def openai_wants_stream(body: bytes) -> bool:
    """True when the JSON body carries "stream": true."""
    try:
        return bool((json.loads(body or b"{}") or {}).get("stream"))
    except Exception:
        return False


async def openai_forward(sub: str, request: Request, api_url: str):
    """Narrow OpenAI passthrough to <api_url>/v1/<sub> (#214).
    Caller has already checked bearer auth + provider-enabled."""
    body = await request.body()
    url = f"{api_url.rstrip('/')}/v1/{sub}"
    headers = {"Content-Type": "application/json"}
    # Blocking requests.post calls run off-loop via run_in_threadpool.
    if openai_wants_stream(body):
        try:
            upstream = await run_in_threadpool(
                lambda: requests.post(url, data=body, headers=headers,
                                      stream=True,
                                      timeout=(5, OPENAI_READ_TIMEOUT_S)))
        except requests.exceptions.RequestException as e:
            raise HTTPException(status_code=502, detail=str(e))
        ctype = upstream.headers.get("content-type") or "text/event-stream"
        if "text/event-stream" not in ctype.lower():
            content, status = upstream.content, upstream.status_code
            upstream.close()
            return Response(content=content, status_code=status, media_type=ctype)
        if not stream_pool.POOL.try_acquire():
            upstream.close()
            raise HTTPException(status_code=503,
                                detail="agent at stream capacity; retry shortly")

        def generate() -> "Iterator[bytes]":
            try:
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return StreamingResponse(stream_pool.guarded_async(generate()),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    try:
        r = await run_in_threadpool(
            lambda: requests.post(url, data=body, headers=headers,
                                  timeout=(5, OPENAI_READ_TIMEOUT_S)))
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=str(e))
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type") or "application/json")


def wrapper_baked_unit_path(wrapper: str) -> str:
    """UNIT_PATH baked into the installed wrapper, or "" if unreadable."""
    try:
        for line in Path(wrapper).read_text().splitlines():
            if line.startswith("UNIT_PATH='") and line.rstrip().endswith("'"):
                return line.rstrip()[len("UNIT_PATH='"):-1]
    except OSError:
        pass
    return ""


def build_svcconfig_tokens(head_tokens: list,
                           args_list: list) -> "Optional[list[str]]":
    """stdin token list for the svcconfig wrapper: head tokens, then one
    flag/value per line. None when any token is non-string or multi-line."""
    tokens = list(head_tokens)
    for a in args_list:
        tokens.append(a["flag"])
        if not a.get("bool") and a.get("value") not in (None, ""):
            tokens.append(str(a["value"]))
    for t in tokens:
        if not isinstance(t, str) or "\n" in t or "\r" in t:
            return None
    return tokens


def svcconfig_apply(wrapper: str, svc_file_path: str, tokens: "list[str]",
                    sudoers_hint: str, restart_unit: "Optional[str]" = None,
                    restart_timeout: int = 30) -> "dict[str, Any]":
    """Baked-unit-mismatch guard → sudo wrapper apply → optional restart."""
    payload = ("\n".join(tokens) + "\n").encode()
    baked = wrapper_baked_unit_path(wrapper)
    if baked and baked != svc_file_path:
        return {"ok": False,
                "error": f"svcconfig helper is baked for {baked} but the configured "
                         f"unit is {svc_file_path} — run a root agent install.sh --update "
                         f"to re-bake the helper and sudoers for the renamed unit"}
    try:
        r = subprocess.run(
            ["sudo", "-n", wrapper],
            input=payload, capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return {"ok": False,
                    "error": r.stderr.decode().strip()
                             or f"svcconfig apply failed — check sudoers for {sudoers_hint}"}
    except Exception as e:
        return {"ok": False, "error": f"Write failed: {e}"}

    if restart_unit:
        try:
            subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", restart_unit],
                           timeout=restart_timeout, check=True, capture_output=True)
        except Exception as e:
            return {"ok": False, "error": f"restart failed: {e}"}
    return {"ok": True}


class LogStream:
    """Popen tail → per-subscriber bounded queues (oldest-line eviction),
    plus the shared SSE endpoint scaffolding (keepalive)."""

    SUB_STALE_S = 60.0

    class _Sub:
        __slots__ = ("queue", "seen")

        def __init__(self, maxsize: int) -> None:
            self.queue: "_queue_lib.Queue[str]" = _queue_lib.Queue(maxsize=maxsize)
            self.seen = time.monotonic()

    def __init__(self, maxsize: int = 4096):
        self.maxsize = maxsize
        self.lock = threading.Lock()
        self.streaming = False
        self._subs: "list[LogStream._Sub]" = []

    def subscribe(self) -> "LogStream._Sub":
        sub = LogStream._Sub(self.maxsize)
        with self.lock:
            self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: "LogStream._Sub") -> None:
        with self.lock:
            if sub in self._subs:
                self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self.lock:
            return len(self._subs)

    def publish(self, line: str) -> None:
        """Deliver one line to every live subscriber; lines with no subscriber are dropped."""
        now = time.monotonic()
        with self.lock:
            self._subs = [s for s in self._subs if now - s.seen <= self.SUB_STALE_S]
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.queue.put_nowait(line)
            except _queue_lib.Full:
                try:
                    sub.queue.get_nowait()  # evict the oldest line
                except _queue_lib.Empty:
                    pass  # drained concurrently; the put below has room
                try:
                    sub.queue.put_nowait(line)
                except _queue_lib.Full:
                    pass  # refilled concurrently; drop this line

    def pump(self, argv: "list[str]", should_keep=None) -> None:
        """Run argv, publish kept stdout lines until stopped."""
        proc = None
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                if not self.streaming:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line or (should_keep is not None and not should_keep(line)):
                    continue
                self.publish(line)
        except Exception as e:
            self.publish(f"[log stream error: {e}]")
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    log.debug("log streamer: terminate failed", exc_info=True)
                try:
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        log.debug("log streamer: kill failed", exc_info=True)
            with self.lock:
                self.streaming = False

    def ensure_started(self, streamer) -> None:
        """Start the streamer thread once."""
        with self.lock:
            if self.streaming:
                return
            self.streaming = True
            threading.Thread(target=streamer, daemon=True).start()

    def _sse_iter(self, sub: "LogStream._Sub", idle_timeout: float = 15.0) -> "Iterator[bytes]":
        """SSE frames from one subscription; unsubscribes when closed."""
        try:
            while True:
                sub.seen = time.monotonic()
                try:
                    line = sub.queue.get(timeout=idle_timeout)
                    yield f"data: {json.dumps({'line': line})}\n\n".encode()
                except _queue_lib.Empty:
                    yield b'data: {"keepalive": true}\n\n'
        finally:
            self.unsubscribe(sub)

    def sse_response(self, streamer=None) -> StreamingResponse:
        """Stream-pool-guarded SSE response fed by a fresh subscription;
        subscribes before starting `streamer` so its first lines aren't missed."""
        sub = self.subscribe()
        if streamer is not None:
            self.ensure_started(streamer)
        return pool_guarded_sse(self._sse_iter(sub))


class JobRunner:
    """Single-subprocess job scaffolding shared by vLLM wizards: busy lock,
    bounded SSE event queue with oldest-drop, cancel event + pgid kill."""

    def __init__(self, name: str, maxsize: int = 5000):
        self.name = name
        self.lock = threading.Lock()
        self.active = False
        self.queue: "_queue_lib.Queue[dict]" = _queue_lib.Queue(maxsize=maxsize)
        self.cancel_event = threading.Event()
        self.proc: "Optional[subprocess.Popen]" = None
        self.pgid: "Optional[int]" = None
        self.thread: "Optional[threading.Thread]" = None

    def try_start(self, target) -> bool:
        """Run target on a daemon thread; False when a job is already active."""
        with self.lock:
            if self.active:
                return False
            self.cancel_event.clear()
            self.active = True
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except Exception:
                break

        def _wrap():
            try:
                target()
            except Exception as e:
                self.put({"type": "done", "ok": False, "error": str(e)})
            finally:
                with self.lock:
                    self.active = False

        self.thread = threading.Thread(target=_wrap, daemon=True)
        self.thread.start()
        return True

    def join(self, timeout: float) -> None:
        """Wait for the job thread to finish (used at agent shutdown)."""
        t = self.thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def put(self, msg: dict) -> None:
        try:
            self.queue.put_nowait(msg)
        except _queue_lib.Full:
            try:
                self.queue.get_nowait()
            except _queue_lib.Empty:
                pass  # queue drained concurrently; nothing to evict
            try:
                self.queue.put_nowait(msg)
            except _queue_lib.Full:
                pass  # still full after evicting one; drop the message

    def track(self, proc: "subprocess.Popen") -> None:
        self.proc = proc
        try:
            self.pgid = os.getpgid(proc.pid)
        except Exception:
            self.pgid = None

    def untrack(self) -> None:
        self.proc = None
        self.pgid = None

    def kill_tracked(self) -> bool:
        """SIGTERM the tracked process group, escalate to SIGKILL after 5s."""
        proc, pgid = self.proc, self.pgid
        if proc is None:
            return False
        try:
            if pgid:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
                return True
            except Exception:
                pass
            if pgid:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
            return True
        except ProcessLookupError:
            return True
        except Exception:
            return False

    def cancel(self) -> bool:
        self.cancel_event.set()
        self.kill_tracked()
        return True

    def _sse_iter(self, idle_timeout: float = 30.0) -> "Iterator[bytes]":
        while True:
            try:
                msg = self.queue.get(timeout=idle_timeout)
            except _queue_lib.Empty:
                if not self.active:
                    yield (b'data: {"type":"done","ok":false,'
                           b'"error":"no active job"}\n\n')
                    return
                yield b'data: {"type":"keepalive"}\n\n'
                continue
            yield f"data: {json.dumps(msg)}\n\n".encode()
            if msg.get("type") == "done":
                return

    def sse_response(self, idle_timeout: float = 30.0) -> StreamingResponse:
        """Stream-pool-guarded SSE response; ends after the done event."""
        return pool_guarded_sse(self._sse_iter(idle_timeout))
