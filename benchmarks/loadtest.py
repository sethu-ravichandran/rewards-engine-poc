"""HTTP load test for POST /api/optimize against a locally started uvicorn.

    python3 benchmarks/loadtest.py --workers 1 --duration 10
    python3 benchmarks/loadtest.py --workers 4 --distinct 20
    python3 benchmarks/loadtest.py --app-dir /path/to/old/checkout   # benchmark another tree

Starts `uvicorn scruge.api:app` (own port, never 8000), then drives it from CLIENT processes
using a minimal stdlib asyncio HTTP/1.1 keep-alive client (httpx would be the bottleneck at these
rates). Reports requests/s and p50/p95/p99 latency. --distinct controls how many different request
bodies are cycled.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing as mp
import os
import random
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WALLET = ["sbi_cashback", "amazon_icici", "hdfc_swiggy", "hdfc_infinia", "tata_neu_rupay"]
MERCH = [("amazon", "5311", "online"), ("swiggy", "5814", "online"), (None, "5411", "tap"),
         ("indian_oil", "5541", "card"), ("croma", "5732", "online"), ("bigbasket", "5411", "upi"),
         (None, "5812", "card"), ("uber", "4121", "online")]


def make_bodies(n: int, path: str, seed: int = 7, paise: bool = False, offers: bool = False) -> list[bytes]:
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        m, mcc, ch = rnd.choice(MERCH)
        txn = {"amount": rnd.choice([99, 250, 640, 1200, 4500, 18000]), "mcc": mcc, "channel": ch}
        if m:
            txn["merchant"] = m
        body = {"transaction": txn, "wallet": WALLET,
                "cycle_usage": {"hdfc_swiggy": {"swiggy": rnd.choice([0, 500, 1500])}}}
        if paise:      # the shape the app sends: integer paise in, lean integer-only response out
            txn["amount_paise"] = txn.pop("amount") * 100
            body["cycle_usage_paise"] = {k: {b: v * 100 for b, v in u.items()} for k, u in body.pop("cycle_usage").items()}
        if offers:
            body["include_offers"] = True
        if path.endswith("/batch"):
            body = {"transactions": [dict(txn, amount=txn["amount"] + i) for i in range(20)],
                    "wallet": WALLET, "sequential": True}
        out.append(json.dumps(body).encode())
    return out


async def client(port: int, path: str, bodies: list[bytes], conns: int, deadline: float, lat: list, errs: list):
    reqs = [(f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
             f"Content-Length: {len(b)}\r\n\r\n").encode() + b for b in bodies]

    async def conn(idx: int):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        i = idx
        while time.perf_counter() < deadline:
            t0 = time.perf_counter()
            w.write(reqs[i % len(reqs)])
            i += conns
            head = await r.readuntil(b"\r\n\r\n")
            status = int(head[9:12])
            clen = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    clen = int(line.split(b":")[1])
            await r.readexactly(clen)
            lat.append(time.perf_counter() - t0)
            if status != 200:
                errs.append(status)
        w.close()

    await asyncio.gather(*(conn(i) for i in range(conns)))


def client_proc(port, path, bodies, conns, deadline, q):
    lat, errs = [], []
    asyncio.run(client(port, path, bodies, conns, deadline, lat, errs))
    q.put((lat, len(errs)))


def pct(sorted_lat, p):
    return sorted_lat[min(len(sorted_lat) - 1, int(len(sorted_lat) * p))] * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--duration", type=float, default=10)
    ap.add_argument("--clients", type=int, default=3, help="client processes")
    ap.add_argument("--conns", type=int, default=16, help="connections per client process")
    ap.add_argument("--distinct", type=int, default=50)
    ap.add_argument("--port", type=int, default=8101)
    ap.add_argument("--path", default="/api/optimize")
    ap.add_argument("--app", default="scruge.api:app")
    ap.add_argument("--app-dir", default=str(ROOT))
    ap.add_argument("--paise", action="store_true", help="send amount_paise/cycle_usage_paise (lean responses), as the app does")
    ap.add_argument("--offers", action="store_true", help="include_offers=true, as the app sends")
    ap.add_argument("--env", action="append", default=[], help="KEY=VAL passed to the server")
    a = ap.parse_args()
    assert a.port != 8000
    env = dict(os.environ, PYTHONPATH=a.app_dir, **dict(e.split("=", 1) for e in a.env))
    log = open(os.devnull, "w")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", a.app, "--port", str(a.port), "--workers", str(a.workers),
                            "--no-access-log", "--log-level", "warning"], cwd=a.app_dir, env=env, stdout=log, stderr=log)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{a.port}/api/meta", timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        bodies = make_bodies(a.distinct, a.path, paise=a.paise, offers=a.offers)
        # warm-up
        wq = mp.Queue()
        p = mp.Process(target=client_proc, args=(a.port, a.path, bodies, 4, time.perf_counter() + 1.5, wq))
        p.start(); wq.get(); p.join()
        q = mp.Queue()
        deadline = time.perf_counter() + a.duration
        procs = [mp.Process(target=client_proc, args=(a.port, a.path, bodies, a.conns, deadline, q)) for _ in range(a.clients)]
        t0 = time.perf_counter()
        for p in procs:
            p.start()
        lat, errs = [], 0
        for _ in procs:
            l, e = q.get(); lat += l; errs += e
        for p in procs:
            p.join()
        el = time.perf_counter() - t0
        lat.sort()
        print(f"workers={a.workers} path={a.path} distinct={a.distinct} env={a.env} clients={a.clients}x{a.conns}conns")
        print(f"requests={len(lat)} non200={errs} rps={len(lat)/el:,.0f} "
              f"p50={pct(lat,.5):.2f}ms p95={pct(lat,.95):.2f}ms p99={pct(lat,.99):.2f}ms")
    finally:
        srv.terminate(); srv.wait(timeout=10)


if __name__ == "__main__":
    main()
