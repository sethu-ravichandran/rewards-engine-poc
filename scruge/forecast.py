"""Forecast the rest of the cycle from a user's own payment history, so the planner knows what is coming.

* Payments are grouped into patterns by (merchant, mcc, channel).
* With completed cycles on record, a pattern's rate is its average per cycle; the number still to come is the smaller of
  rate x fraction of the cycle left and rate - already done. A new user's rate is extrapolated from the elapsed fraction.
* Each expected payment uses the pattern's median amount and is spread evenly over the remaining days.
* The forecast is capped in size and keeps the largest payments, since that is where caps matter.

Confidence is reported: none (no history), low (this cycle only), medium (1-2 completed cycles), high (3+).
All money is integer paise.
"""
from __future__ import annotations

import calendar
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Channel, Transaction


@dataclass(frozen=True)
class Pattern:
    merchant: str | None
    mcc: str
    channel: str
    per_cycle: float               # average payments per full cycle
    median_amount_paise: int
    done_this_cycle: int
    expected_remaining: int
    basis: str                     # "history" (completed cycles) | "this_cycle" (extrapolated)

    def as_dict(self) -> dict:
        return {"merchant": self.merchant, "mcc": self.mcc, "channel": self.channel, "per_cycle": round(self.per_cycle, 2),
                "median_amount_paise": self.median_amount_paise, "done_this_cycle": self.done_this_cycle,
                "expected_remaining": self.expected_remaining, "basis": self.basis}


@dataclass(frozen=True)
class Forecast:
    transactions: tuple[Transaction, ...]
    patterns: tuple[Pattern, ...]
    confidence: str                # none | low | medium | high
    cycles_observed: int
    note: str

    def as_dict(self) -> dict:
        return {"payments": [{"amount_paise": t.amount_paise, "mcc": t.mcc, "channel": t.channel.value, "merchant": t.merchant}
                             for t in self.transactions],
                "patterns": [p.as_dict() for p in self.patterns], "confidence": self.confidence,
                "cycles_observed": self.cycles_observed, "note": self.note}


def _round_half_up(x: float) -> int:
    return int(x + 0.5)


def forecast(rows: list[dict], now: datetime | None = None, max_payments: int = 29) -> Forecast:
    """`rows`: ledger rows (dicts with cycle, merchant, mcc, channel, amount_paise)."""
    now = now or datetime.now(timezone.utc)
    cur = now.strftime("%Y-%m")
    dim = calendar.monthrange(now.year, now.month)[1]
    elapsed = max(now.day / dim, 1 / dim)
    remaining_frac = (dim - now.day) / dim
    if not rows:
        return Forecast((), (), "none", 0, "No payment history yet, so there is nothing to forecast.")

    completed = sorted({r["cycle"] for r in rows if r["cycle"] < cur})
    n_done = len(completed)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["merchant"], r["mcc"], r["channel"])].append(r)

    patterns: list[Pattern] = []
    for (merchant, mcc, channel), rs in groups.items():
        done = sum(1 for r in rs if r["cycle"] == cur)
        if n_done:
            past = [r for r in rs if r["cycle"] < cur]
            per_cycle = len(past) / n_done
            if per_cycle <= 0:
                continue
            remaining = max(0, min(_round_half_up(per_cycle * remaining_frac), _round_half_up(per_cycle - done)))
            basis = "history"
        else:
            per_cycle = done / elapsed
            remaining = min(done, _round_half_up(per_cycle * remaining_frac))
            basis = "this_cycle"
        if remaining <= 0:
            continue
        amount = int(statistics.median_low(r["amount_paise"] for r in rs))
        patterns.append(Pattern(merchant, mcc, channel, per_cycle, amount, done, remaining, basis))

    # spread each pattern's expected payments evenly over the remaining days; order = rhythm of the person
    expanded: list[tuple[float, int, Pattern]] = []
    span = max(dim - now.day, 1)
    for i, p in enumerate(sorted(patterns, key=lambda p: (-p.median_amount_paise * p.expected_remaining, p.mcc, str(p.merchant), p.channel))):
        for k in range(p.expected_remaining):
            expanded.append((now.day + (k + 0.5) * span / p.expected_remaining, i, p))
    if len(expanded) > max_payments:          # keep the biggest payments: that is where caps matter
        keep = sorted(expanded, key=lambda e: -e[2].median_amount_paise)[:max_payments]
        expanded = keep
    expanded.sort(key=lambda e: (e[0], e[1]))
    txns = tuple(Transaction(p.median_amount_paise, p.mcc, Channel(p.channel), p.merchant) for _, _, p in expanded)

    confidence = "low" if n_done == 0 else ("medium" if n_done < 3 else "high")
    basis_note = (f"{n_done} completed cycle{'s' if n_done != 1 else ''} of history" if n_done
                  else "this cycle only (extrapolated, so treat the advice as a hint)")
    note = (f"Forecast of {len(txns)} more payment{'s' if len(txns) != 1 else ''} this cycle, based on {basis_note}." if txns
            else f"Nothing more expected this cycle, based on {basis_note}.")
    return Forecast(txns, tuple(sorted(patterns, key=lambda p: (-p.median_amount_paise * p.expected_remaining, p.mcc))),
                    confidence, n_done, note)
