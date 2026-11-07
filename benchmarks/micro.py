"""Micro-benchmark of optimize() for a 5-card wallet: frozen reference engine vs current engine.

    python3 benchmarks/micro.py [--n 200000]

Reports ns/call (median of 5 repeats) over a fixed mix of 12 transactions, with and without
explanation strings. Pure engine cost: no HTTP, no pydantic, no JSON.
"""
from __future__ import annotations

import argparse
import inspect
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scruge import engine as cur  # noqa: E402
from scruge.catalog import load_cards  # noqa: E402
from scruge.models import Channel, CycleState, Transaction  # noqa: E402
from tests import reference_engine as ref  # noqa: E402

CARDS = load_cards()
WALLET = list(CARDS.values())
O, C, T, U = Channel.ONLINE, Channel.CARD, Channel.TAP, Channel.UPI
TXNS = [
    Transaction(250_000, "5311", O, "amazon"), Transaction(100_000, "5814", O, "swiggy"),
    Transaction(500_000, "5411", T, None), Transaction(60_000, "5541", C, "indian_oil"),
    Transaction(2_000_000, "5732", O, "croma"), Transaction(50_000, "5411", U, "bigbasket"),
    Transaction(5_000_000, "6513", O, "rent"), Transaction(123_456, "5812", C, None),
    Transaction(80_000, "4121", O, "uber"), Transaction(900_000, "7011", O, "smartbuy_hotels"),
    Transaction(30_000, "5999", O, None), Transaction(1_500_000, "5651", O, "myntra"),
]
STATES = {"hdfc_swiggy": CycleState({"swiggy": 100_000, "online_5": 20_000}),
          "sbi_cashback": CycleState({"cashback_total": 300_000})}


def bench(fn, n: int, repeat: int = 5) -> float:
    k = len(TXNS)
    runs = []
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for i in range(n):
            fn(TXNS[i % k])
        runs.append((time.perf_counter_ns() - t0) / n)
    return statistics.median(runs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100_000)
    n = ap.parse_args().n
    has_explain = "explain" in inspect.signature(cur.optimize).parameters
    rows = [("reference (original) optimize", bench(lambda t: ref.optimize(WALLET, t, STATES), n))]
    if has_explain:
        rows.append(("current optimize explain=True", bench(lambda t: cur.optimize(WALLET, t, STATES), n)))
        rows.append(("current optimize explain=False", bench(lambda t: cur.optimize(WALLET, t, STATES, explain=False), n)))
    else:
        rows.append(("current optimize (no explain flag)", bench(lambda t: cur.optimize(WALLET, t, STATES), n)))
    base = rows[0][1]
    print(f"python {sys.version.split()[0]}, 5-card wallet, {n} calls x5 repeats, median ns/call")
    for name, v in rows:
        print(f"{name:40s} {v:10.0f} ns/call  {v/1000:8.2f} us  x{base/v:5.2f} vs reference")


if __name__ == "__main__":
    main()
