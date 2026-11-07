"""Observability: JSON logging with request ids, and the edge ASGI middleware.

The middleware is plain ASGI (not BaseHTTPMiddleware, which costs an extra task + queue hop per
request). It: assigns/propagates X-Request-Id, enforces the body-size limit, times the request
(Server-Timing), stamps X-Rules-Version on /api/* responses, writes an access-log line, and converts any unhandled exception into a generic 500 (details only in the server log).
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import sys
import time
from typing import Callable

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
access_log = logging.getLogger("scruge.access")
log = logging.getLogger("scruge")
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name,
             "msg": record.getMessage(), "request_id": request_id_var.get()}
        extra = getattr(record, "fields", None)
        if extra:
            d.update(extra)
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        return json.dumps(d, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger("scruge")
    root.setLevel(level.upper())
    # Access lines are already JSON (see EdgeMiddleware); pass them through instead of re-encoding.
    acc = logging.getLogger("scruge.access")
    if not acc.handlers:
        ah = logging.StreamHandler(sys.stderr)
        ah.setFormatter(logging.Formatter("%(message)s"))
        acc.addHandler(ah)
        acc.propagate = False
    if not any(getattr(h, "_scruge", False) for h in root.handlers):
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(JsonFormatter())
        h._scruge = True  # type: ignore[attr-defined]
        root.addHandler(h)
        root.propagate = False


async def _json(send, status: int, payload: dict, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), *(extra or [])]})
    await send({"type": "http.response.body", "body": body})


class EdgeMiddleware:
    def __init__(self, app, get_version: Callable[[], str], max_body_bytes: int = 1_000_000,
                 access_log_enabled: bool = True) -> None:
        self.app, self.get_version = app, get_version
        self.max_body, self.access = max_body_bytes, access_log_enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        rid = ""
        clen = None
        for k, v in scope["headers"]:
            if k == b"x-request-id":
                rid = v.decode("latin-1")
            elif k == b"content-length":
                clen = v
        if not _RID.match(rid):
            rid = os.urandom(8).hex()
        token = request_id_var.set(rid)
        path = scope["path"]
        status = 500
        rid_b = rid.encode()
        ver = self.get_version().encode() if path.startswith("/api/") else None

        async def send_wrap(msg):
            nonlocal status
            if msg["type"] == "http.response.start":
                status = msg["status"]
                dur_ms = (time.perf_counter() - t0) * 1000
                msg = dict(msg)
                msg["headers"] = [*msg["headers"], (b"x-request-id", rid_b),
                                  (b"server-timing", f"app;dur={dur_ms:.2f}".encode())]
                if ver is not None:
                    msg["headers"].append((b"x-rules-version", ver))
            await send(msg)

        try:
            if clen is not None and clen.isdigit() and int(clen) > self.max_body:
                await _json(send_wrap, 413, {"detail": f"request body too large (max {self.max_body} bytes)"})
            else:
                await self.app(scope, receive, send_wrap)
        except Exception:
            log.exception("unhandled error on %s %s", scope["method"], path)
            if status == 500:  # response not started yet
                try:
                    await _json(send_wrap, 500, {"detail": "internal server error", "request_id": rid})
                except Exception:
                    pass
        finally:
            dur = time.perf_counter() - t0
            if self.access and access_log.isEnabledFor(logging.INFO):
                access_log.info('{"ts":%.3f,"level":"INFO","logger":"scruge.access","request_id":"%s","method":"%s",'
                                '"path":%s,"status":%d,"dur_ms":%.3f}' % (
                                    time.time(), rid, scope["method"], json.dumps(path), status, dur * 1000))
            request_id_var.reset(token)
