"""Where does a /api/optimize request spend its time?

    python3 benchmarks/profile_hotpath.py [--app-dir DIR] [--n 20000] [--cprofile]

Drives the ASGI app in-process (no sockets, no uvicorn) with a minimal ASGI driver, so the
numbers cover framework + validation + engine + serialisation but not the HTTP server/parser.
"""
from __future__ import annotations

import argparse
import asyncio
import cProfile
import importlib
import json
import pstats
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--app-dir", default=str(Path(__file__).resolve().parent.parent))
ap.add_argument("--app", default="scruge.api:app")
ap.add_argument("--n", type=int, default=20000)
ap.add_argument("--cprofile", action="store_true")
ap.add_argument("--paise", action="store_true", help="send amount_paise/cycle_usage_paise (lean response) instead of the legacy rupee fields")
ap.add_argument("--path", default="/api/optimize")
a = ap.parse_args()
sys.path.insert(0, a.app_dir)
mod, attr = a.app.split(":")
app = getattr(importlib.import_module(mod), attr)

BODY = json.dumps({
    "transaction": {"amount": 1000, "mcc": "5814", "channel": "online", "merchant": "swiggy"},
    "wallet": ["sbi_cashback", "amazon_icici", "hdfc_swiggy", "hdfc_infinia", "tata_neu_rupay"],
    "cycle_usage": {"hdfc_swiggy": {"swiggy": 500}}}).encode()
if a.paise:
    _b = json.loads(BODY)
    _b["transaction"] = {"amount_paise": 100_000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}
    _b["cycle_usage_paise"] = {"hdfc_swiggy": {"swiggy": 50_000}}; _b.pop("cycle_usage")
    BODY = json.dumps(_b).encode()
if a.path.endswith("/batch"):
    t = {"amount": 1000, "mcc": "5814", "channel": "online", "merchant": "swiggy"}
    BODY = json.dumps({"transactions": [t] * 20, "wallet": json.loads(BODY)["wallet"], "sequential": True}).encode()
HDRS = [(b"host", b"x"), (b"content-type", b"application/json"), (b"content-length", str(len(BODY)).encode())]


async def one() -> int:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": a.path, "raw_path": a.path.encode(), "query_string": b"", "headers": HDRS,
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "root_path": ""}
    sent = False
    out = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": BODY, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(m):
        out.append(m)

    await app(scope, receive, send)
    return out[0]["status"]


async def run(n):
    for _ in range(n):
        await one()


async def main():
    assert await one() == 200
    await run(500)
    t0 = time.perf_counter()
    await run(a.n)
    dt = time.perf_counter() - t0
    print(f"{a.path}: {dt / a.n * 1e6:.1f} us/request in-process ({a.n / dt:,.0f} req/s single thread)")
    if a.cprofile:
        pr = cProfile.Profile()
        pr.enable()
        await run(5000)
        pr.disable()
        pstats.Stats(pr).sort_stats("tottime").print_stats(18)


asyncio.run(main())
